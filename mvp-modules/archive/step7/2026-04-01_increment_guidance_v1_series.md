# Step 7 v01→v02 Increment Guidance

## What this is

Scaffolding for a safe, verifiable progression from `voice_pipeline_step7_v01.py` to `v02.py`.
The existing `v02.py` contains a VAD bug that silently breaks speech detection. This sequence
fixes it inline so the end state matches v02's intent without the bug.

**Working directory:** `step7_working/`
**Reference files:** `v01.py` (baseline), `v02.py` (target with bug), `v07.py` (GatedVAD reference)

---

## VAD Breakage — Root Cause (read this first)

`v02.py` introduces `GatedVADProcessor`. Its `__init__` registers two event handlers:

```python
@self._vad_controller.event_handler("on_speech_started")
async def on_speech_started(_controller):
    print(f"  [VAD] speech_started (after {self._vad_frame_count} frames to Silero)")
```

`self._vad_frame_count` is **never initialized** in `v02.__init__`. First speech detection
fires `on_speech_started` → `AttributeError: 'GatedVADProcessor' object has no attribute
'_vad_frame_count'` → VAD silently dead for the session.

**Fix (two lines):**
1. Add `self._vad_frame_count = 0` to `GatedVADProcessor.__init__`
2. Use audio-only gating (see pattern below)

**Secondary issue in v02:** `process_frame` only sends `StartFrame` + audio-when-capturing to
`_vad_controller`. Non-audio frames (EndFrame, CancelFrame, SystemFrame) never reach the
controller — it may miss lifecycle events. Fix: gate audio-only, always forward everything else.

---

## The Audio-Only Gating Pattern (from v07.py lines 139–156)

```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    await super().process_frame(frame, direction)
    await self.push_frame(frame, direction)          # always forward downstream

    if isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
        # Only audio frames are gated
        if self._capturer.capturing:
            await self._vad_controller.process_frame(frame)
    else:
        # Non-audio frames always reach the controller (lifecycle events)
        await self._vad_controller.process_frame(frame)
```

Note: `StartFrame` handling is implicit — it's not `AudioRawFrame`, so it falls into the
`else` branch and always reaches the controller. No special case needed.

---

## Sequence Overview

| Stage | File | One-line description | Status |
|-------|------|----------------------|--------|
| 1 | v01a | Gate OWW during capturing/processing — no VAD change | **DONE** ✓ VAD fired, Ctrl+C crashed |
| 2 | v01b | GatedVADProcessor with speech prints — functional + visible | **DONE** ✓ VAD fires, Ctrl+C crash reoccurs |
| 3 | ~~v01c~~ | ~~Add speech_started / speech_stopped prints~~ | **collapsed into v01b** |
| 4 | v02  | Reconcile against existing v02.py | **DONE** — v01b IS v02 + fixes (see below) |

Each stage has its own markdown in `increments/`.

## v01a Results (2026-04-01)
- VAD fired as expected — no regression introduced by OWW gating
- Ctrl+C crashed (queue overflow with Silero unbounded during cognitive loop)
- Ready for v01b

## v01b Corrections vs. Original Spec (2026-04-01)

**Root cause of VAD breakage (first v01b attempt):** `GatedVADProcessor` had
`on_push_frame` / `on_broadcast_frame` handlers but no `on_speech_stopped` handler.
`VADController` fires `on_speech_stopped` for speech transitions — it never calls
`push_frame()` / `broadcast_frame()` internally for VAD state changes. Without
`on_speech_stopped` → `broadcast_frame(VADUserStoppedSpeakingFrame, ...)`,
`UtteranceCapturer` never triggers. `on_push_frame` is only for controller-initiated
frame pushes (e.g. `SpeechControlParamsFrame` from `StartFrame` handling).

**Increment guidance was wrong:** v01c was labelled "visibility layer" for prints, but
`on_speech_started` / `on_speech_stopped` handlers are the **functional emission mechanism**.
Rule: include prints as soon as there is a handler — no deferral.

**`process_frame` simplification:** Original v01b spec used audio/non-audio split.
Replaced with v04's pattern: `StartFrame` always, else gate on capturing.
`VADController` only processes `StartFrame`, `InputAudioRawFrame`, `VADParamsUpdateFrame`
— forwarding all non-audio frames was unnecessary.

## v01b Results (2026-04-01)
- VAD fires ✓ (functional — no regression)
- Ctrl+C crash reoccurs — Silero gating was not the sole cause; separate fix needed
- Duplicate wake word detection → bad state observed (fix planned for 02-03 increment)
- Sensitivity note: needs to speak loudly at mic — worth monitoring

## v02 Reconciliation (2026-04-01)
v01b is structurally identical to v02 plus the two bug fixes. Residual diff is cosmetic only:

| Location | v01b | v02 |
|----------|------|-----|
| `__init__` | `self._vad_frame_count = 0` ← **fix** | missing → AttributeError |
| `process_frame` | `self._vad_frame_count += 1` ← **fix** | missing |
| OWW comment | "Silero VAD and the Claude subprocess" | "the claude subprocess" |
| imports | one combined `frame_processor` line | two separate lines |
| docstring | full change history | minimal |
| banner | `v01b --` | no version tag |

No structural work needed. v02 reconciliation complete.

---

## Key Pipeline Architecture

```
transport.input()
    → GatedVADProcessor    # Silero ONNX — only runs during capturer.capturing
    → OpenWakeWordProcessor # OWW ONNX  — skips when capturer.processing or .capturing
    → UtteranceCapturer    # audio buffer + cognitive loop trigger
```

**Why VAD goes first:** `VADUserStoppedSpeakingFrame` must propagate downstream to
`UtteranceCapturer`. VAD must be upstream of the capturer.

**Why OWW goes second:** OWW runs on raw audio. It doesn't need VAD frames. Order between
VAD and OWW is for pipeline position only; they are independent.

**Construction order in `main()`:** `capturer` must be constructed before `GatedVADProcessor`
because the processor holds a reference to `capturer.capturing`.

---

## CPU Budget on Pi 4

| Processor | Active phase | Cost per invocation |
|-----------|-------------|---------------------|
| Silero VAD | CAPTURING only | 15–25ms (ONNX) |
| OWW predict | LISTENING only | 20–40ms per 80ms window |
| UtteranceCapturer | CAPTURING only | <1ms (list.append) |

Frame interval: 20ms (PyAudio hardware-driven, unbounded queue).
OWW accumulates 4 frames (1280 samples = 80ms) before each predict — budget is 80ms not 20ms,
but only if the 3 intervening frames pass cheaply.

Gating both processors to non-overlapping phases is the core fix.

---

## Observable Failure Signatures

| Symptom | Likely cause |
|---------|-------------|
| `[VAD] speech_started` never prints | VAD broken — AttributeError in handler, or not gated into |
| `[VAD] speech_stopped` never prints | Same as above, or speech didn't meet start_secs threshold |
| Wake detected but VAD never fires | GatedVADProcessor not receiving frames during capturing |
| Reboot on Ctrl+C after Claude call | Queue overflow — OWW or Silero still running during loop |
| False wake detection post-loop | OWW stale preprocessor buffers (v03 fix territory) |

---

## Imports Required by v01b+

```python
from pipecat.frames.frames import Frame, AudioRawFrame, InputAudioRawFrame, StartFrame, VADUserStoppedSpeakingFrame
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.vad.vad_controller import VADController
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
```

Drop: `from pipecat.processors.audio.vad_processor import VADProcessor` (no longer needed).
