# Step 7 Crash Analysis — Consolidated

**Date:** 2026-04-01
**Scope:** Troubleshooting effort to eliminate Pi 4 reboots when introducing the agentic layer (`claude -p`) into the Pipecat voice pipeline.

---

## The Problem

Adding a Claude subprocess to a working wake-word + VAD + STT pipeline (steps 1–6) caused the Pi 4 to hard-reboot. The failure signature was consistent across all attempts: `client_loop: send disconnect: Connection reset` after Ctrl+C, following any session where the cognitive loop ran. Ctrl+C before any cognitive loop always exited cleanly.

This is not a software crash. The Pi itself goes down — SSH session dies because the device reboots.

---

## Root Cause: Unbounded Queues + CPU Starvation + USB Audio

### Frame production rate

PyAudio's callback thread produces one `InputAudioRawFrame` every **20ms**, driven by the ALSA/USB audio subsystem. This is hardware-paced and not governed by the asyncio event loop. Frames are pushed via `asyncio.run_coroutine_threadsafe()`.

### Queue architecture

Every level of Pipecat uses unbounded `asyncio.Queue` instances — no backpressure, no dropping, no bound. When any processor's `process_frame` takes longer than the frame interval, frames accumulate without limit.

### Per-frame CPU budget

| Processor | Phase active | Approx cost per invocation |
|-----------|-------------|---------------------------|
| Silero VAD (GatedVADProcessor) | CAPTURING only | 15–25ms (ONNX) |
| OpenWakeWord (OWW) | LISTENING only | 20–40ms per 80ms window |
| UtteranceCapturer | CAPTURING only | <1ms (list.append) |

Frame interval is 20ms. OWW accumulates 4 frames (1280 samples, 80ms of audio) per predict call — its effective budget is 80ms, not 20ms — but only if the 3 intervening frames pass through cheaply.

### The cascade

When the Claude subprocess runs (7–12s), it consumes significant CPU. Any ONNX inference still running during that window competes for the remaining cycles. Frames accumulate. On Ctrl+C, Pipecat attempts to flush its queues; the asyncio teardown cannot complete while PyAudio's callback thread keeps firing and the USB isochronous transfer layer is starved. The kernel panics or the USB subsystem hangs. Hard reboot.

---

## Attempt History

### v01 — Full step 7 script (FAILED)

OWW `predict()` ran on every audio frame throughout all pipeline phases, including the 7–12s cognitive loop. Claude subprocess + OWW ONNX competed for CPU. Frames accumulated, Ctrl+C triggered the reboot. Clean exit when no Claude call was made.

**Learning:** CPU contention during the cognitive loop is the trigger. Any ONNX inference running concurrently with the subprocess will cause accumulation.

---

### v01a — Gate OWW during cognitive loop (FAILED)

Added a gate: OWW skips `predict()` when `capturer.processing or capturer.capturing`. Clears `self._chunks` on skip so stale audio does not contaminate the next predict window.

Silero VAD (`VADProcessor`, Pipecat built-in) was left unchanged — it ran ONNX inference on every frame regardless of turn state. Two concurrent ONNX workloads during CAPTURING exceeded the budget. Reboot on Ctrl+C still occurred.

**Learning:** OWW gating alone is insufficient. Silero running ungated during CAPTURING is itself enough to cause accumulation.

---

### v01b/v02 — GatedVADProcessor + gate both processors (PARTIAL)

Replaced Pipecat's built-in `VADProcessor` with a custom `GatedVADProcessor` that wraps `VADController` (Silero) but only feeds audio frames to it when `capturer.capturing` is True. Both ONNX workloads now run in non-overlapping phases:

- **LISTENING:** OWW runs, Silero does not
- **CAPTURING:** Silero runs, OWW does not
- **PROCESSING (cognitive loop):** neither runs

Three bugs were found and fixed during this increment:

#### Bug 1: Missing `_vad_frame_count` init → silent VAD death
`GatedVADProcessor.__init__` registered `on_speech_started` / `on_speech_stopped` handlers that referenced `self._vad_frame_count`, but the attribute was never initialized. First speech detection fired the handler → `AttributeError` → VAD silently dead for the session. The cognitive loop never started. The failure was invisible — wake word fired, audio was captured, but nothing happened.

**Fix:** `self._vad_frame_count = 0` in `__init__`, before any handler registration.

#### Bug 2: Missing `on_speech_stopped` handler → cognitive loop never triggered
The initial `GatedVADProcessor` only registered `on_push_frame` and `on_broadcast_frame`. `VADController` fires `on_speech_stopped` for speech-end transitions. Without an `on_speech_stopped` handler calling `broadcast_frame(VADUserStoppedSpeakingFrame, ...)`, the frame was never emitted and `UtteranceCapturer` never triggered.

**Fix:** `on_speech_started` and `on_speech_stopped` are the **functional emission mechanism**, not optional observability. Include them from the start.

#### Bug 3: Non-audio frames not forwarded to VADController
An early version of `process_frame` only forwarded `StartFrame` explicitly and audio-when-capturing. All other non-audio frames (`EndFrame`, `CancelFrame`, `SystemFrame`) never reached the controller.

**Fix — audio-only gating pattern:**
```python
async def process_frame(self, frame, direction):
    await super().process_frame(frame, direction)
    await self.push_frame(frame, direction)          # always forward downstream

    if isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
        if self._capturer.capturing:
            self._vad_frame_count += 1
            await self._vad_controller.process_frame(frame)
    else:
        await self._vad_controller.process_frame(frame)  # lifecycle events always reach controller
```

**Result after fixes:** Both OWW and Silero gated to non-overlapping phases. VAD fires, cognitive loop triggers, duplicate wake word detection observed (addressed in v03). Ctrl+C reboot still occurs.

---

### v03 — Full OWW state reset on ungating (PARTIAL)

After gating resolved the CPU contention issue, a new failure emerged: false wake detection without any wake word spoken. Score: 0.865. Root cause: OWW's preprocessor feature buffers (`prediction_buffer`, `raw_data_buffer`, `melspectrogram_buffer`, `feature_buffer`, `accumulated_samples`) accumulated stale state during the gating period. When gating lifted, stale features combined with fresh audio to score a false positive. Pipeline entered CAPTURING with nobody speaking.

**Fix:** Full model state reset on every ungating event — clear all five internal buffers.

**Result:** False wake detection eliminated. But Ctrl+C after a completed cognitive loop still reboots the Pi. The stale-state problem was distinct from the shutdown problem.

---

### v04 — Stop PyAudio stream before teardown (TESTING)

Hypothesis: the reboot is caused by PyAudio's callback thread continuing to fire during asyncio teardown. When Ctrl+C arrives and the pipeline cancels, Pipecat processes a `CancelFrame` through the chain. If PyAudio is still producing frames during this window, the USB isochronous transfer layer starves and cascades.

**Approach:** Patch `LocalAudioInputTransport.cancel()` to call `stop_stream()` immediately when a `CancelFrame` is received, before the normal cancel path runs. A 100ms drain sleep lets already-queued frames propagate through.

Status: testing. This is the current candidate fix for the fundamental shutdown race.

---

## What Is Confirmed to Work (Steps 1–6)

These operations are individually stable on Pi 4 under Pipecat and survive Ctrl+C cleanly:

- OWW wake word detection running indefinitely in LISTENING
- Silero VAD detecting speech boundaries during CAPTURING
- Deepgram STT (cloud call, minimal CPU)
- Audio capture using `list.append` + deferred `np.concatenate`
- `PipelineRunner` SIGINT handling when no cognitive loop has run

The instability is specific to the interaction between the cognitive loop's CPU footprint and the pipeline's continued operation during and after that loop.

---

## Pipeline Architecture (Current)

```
transport.input()
    → GatedVADProcessor    # Silero ONNX — only during capturer.capturing
    → OpenWakeWordProcessor # OWW ONNX  — skipped during capturer.processing or .capturing
    → UtteranceCapturer    # audio buffer + cognitive loop trigger
```

**Construction order matters:** `capturer` must be instantiated before `GatedVADProcessor` because the processor holds a reference to `capturer.capturing`.

---

## Key Learnings

1. **Any ONNX inference running concurrently with the Claude subprocess is fatal on Pi 4.** The CPU budget is exhausted; queues grow; USB audio cascades. Gate everything.

2. **Both ONNX workloads must run in non-overlapping phases.** OWW in LISTENING, Silero in CAPTURING, neither during PROCESSING. This is the architectural fix, not a workaround.

3. **`VADController` event handlers are the functional emission mechanism.** `on_speech_stopped` must `broadcast_frame(VADUserStoppedSpeakingFrame)`. Without it, VAD is wired up but the downstream capturer never sees the trigger. This is easy to miss because the pipeline appears functional — wake word fires, audio captures — until you notice the cognitive loop never starts.

4. **Attribute initialization must precede handler registration.** Handlers that reference instance attributes will fail on first invocation if those attributes aren't initialized in `__init__`. The failure is silent — an `AttributeError` in an async handler raises but doesn't propagate visibly.

5. **OWW preprocessor buffers must be reset on ungating.** Features accumulated during the gating period are stale relative to fresh audio. When gating lifts, the model scores a blend of stale and fresh features, producing false positives. Reset all five internal buffers (`prediction_buffer`, `raw_data_buffer`, `melspectrogram_buffer`, `feature_buffer`, `accumulated_samples`) on each ungate event.

6. **The 20ms frame interval and the 80ms OWW window are both relevant.** 20ms: the hardware-driven rate at which queues receive new items. 80ms: OWW's processing cycle (1280 samples). OWW's effective budget is 80ms, but only if the 3 intervening frames pass cheaply. Both determine queue growth rate.

7. **Ctrl+C before any cognitive loop always exits cleanly; after is fatal.** The shutdown race is specifically introduced by having run a cognitive loop. Shutdown is the stress test.

8. **Diagnostic prints in VAD event handlers are essential observability.** Silent VAD failure (no `[VAD] speech_started`, no `[VAD] speech_stopped`) is indistinguishable from "nobody is speaking" without them. Include them from the first implementation, not as a later addition.

---

## Open Questions

- Does v04's PyAudio `stop_stream()` on `CancelFrame` prevent the USB cascade, or does the problem lie in residual state from `asyncio.to_thread` / `asyncio.create_task` after the cognitive loop?
- Is there background CPU pressure from the cognitive loop that makes OWW's resumed predict calls slower post-loop than pre-loop?
- Would a bounded transport queue with frame-dropping address the root cause, or just slow the accumulation?
- Is pausing/stopping the audio stream during the cognitive loop and restarting after the correct long-term architecture?

---

## Success Criteria

1. Complete a full voice turn (wake → capture → STT → Claude → return to listening) and remain healthy.
2. Survive 3–5 consecutive turns without degradation.
3. Shut down cleanly on Ctrl+C at any point in the turn cycle, including after completed cognitive loops.
4. Not reboot the Pi under any normal operating condition.
