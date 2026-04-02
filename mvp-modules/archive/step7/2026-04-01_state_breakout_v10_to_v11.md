# State Object Breakout: v10 → v11

**Files:** `step7_working/voice_pipeline_step7_v10.py` → `step7_working/voice_pipeline_step7_v11.py`

> Note: v10's internal docstring calls itself "v09". v11's docstring credits "v09 + v10 selective conditional". For this report, v10 means the file named v10.

---

## What changed in one sentence

v11 extracts all shared session state from `UtteranceCapturer` (and the scattered bits in `GatedVADProcessor`) into a single `PipelineState` object, and makes every processor reference that object rather than each other.

---

## v10 state ownership (before)

In v10, shared session state is split across two processors that hold direct references to each other:

**`UtteranceCapturer`** owns the authority state:
```
self.capturing   : bool   — True between wake-word and VAD stop-speaking
self.processing  : bool   — True during _cognitive_loop execution
self._chunks     : list   — accumulated audio
self._vad_weak   : weakref → GatedVADProcessor   (to call reset_vad())
```

**`GatedVADProcessor`** owns the counters:
```
self._vad_frame_count   : int  — frames fed to Silero this utterance
self._total_frame_count : int  — all audio frames seen since start
```
It also holds a **direct strong reference** to `capturer` to read `capturer.capturing`.

**`OpenWakeWordProcessor`** holds a **direct strong reference** to `capturer` to call `capturer.start_capture()`.

### Cross-reference graph (v10)

```
OpenWakeWordProcessor ──→ UtteranceCapturer (direct ref)
GatedVADProcessor     ──→ UtteranceCapturer (direct ref)
UtteranceCapturer     ──→ GatedVADProcessor (weakref, _vad_weak)
```

State mutations are scattered:
- `start_capture()` on `UtteranceCapturer` sets `self.capturing = True`, triggers `vad.reset_vad()` via weakref
- `process_frame` on `UtteranceCapturer` clears `capturing`, sets `processing = True`
- `_cognitive_loop` `finally` block sets `self.processing = False` and calls `vad.reset_vad()` again via weakref
- `GatedVADProcessor.reset_vad()` resets Silero states AND resets its own `_vad_frame_count`

---

## v11 state ownership (after)

A new `PipelineState` class becomes the single source of truth. All processors receive only `state` in their constructors — no processor holds a reference to another processor.

### `PipelineState` fields

```python
_phase              : str        — "idle" | "capturing" | "processing"
_vad_frame_count    : int        — frames fed to Silero this utterance
_total_frame_count  : int        — all audio frames seen
_transport_ref      : object     — strong ref to input_transport (long-lived, no cycle)
_vad_ref            : weakref    — → GatedVADProcessor
_capturer_ref       : weakref    — → UtteranceCapturer
```

### Read-only properties exposed

```python
state.phase          → str
state.capturing      → bool  (phase == "capturing")
state.processing     → bool  (phase == "processing")
state.vad_frame_count
state.total_frame_count
```

### Cross-reference graph (v11)

```
OpenWakeWordProcessor ──→ PipelineState
GatedVADProcessor     ──→ PipelineState
UtteranceCapturer     ──→ PipelineState
PipelineState         ──→ GatedVADProcessor  (weakref, _vad_ref)
PipelineState         ──→ UtteranceCapturer  (weakref, _capturer_ref)
PipelineState         ──→ input_transport    (strong ref, _transport_ref)
```

No processor-to-processor references remain.

---

## Phase transitions and the `set_phase()` policy

v11 centralises all transition side-effects in `PipelineState.set_phase()`:

| Transition | Side effects scheduled (as async tasks) |
|---|---|
| any → `capturing` | `_vad_frame_count = 0`; `_do_vad_reset()` (Silero hidden states) |
| any → `processing` | `_do_pause_stream()` — stops PyAudio input stream |
| `processing` → `idle` | `_vad_frame_count = 0`; `_do_reset_then_resume()` — Silero reset then stream start |

All stream ops are scheduled via `asyncio.create_task()`, never called synchronously from within a Pipecat frame-processing callback (avoids PortAudio deadlock / USB fault on Pi).

`_do_reset_then_resume()` sequences Silero reset **before** stream resume — this ordering prevents stale LSTM state from contaminating the first frames of the next utterance.

---

## Per-processor diff summary

### `GatedVADProcessor`

| v10 | v11 |
|---|---|
| `__init__(self, *, vad_analyzer, capturer, ...)` | `__init__(self, *, vad_analyzer, state, ...)` |
| Holds `self._capturer` (direct ref) | Holds `self.state` only |
| Owns `_vad_frame_count`, `_total_frame_count` | Calls `state.inc_vad_frames()`, `state.inc_total_frames()` |
| `reset_vad()` also resets `self._vad_frame_count = 0` | `reset_vad()` only resets Silero model; counter reset owned by state |
| Reads `self._capturer.capturing` | Reads `self.state.capturing` |

### `OpenWakeWordProcessor`

| v10 | v11 |
|---|---|
| `__init__(self, capturer)` | `__init__(self, state)` |
| Calls `self.capturer.start_capture()` on wake | Calls `self.state.request_capture()` on wake |
| Reads `self.capturer.processing or self.capturer.capturing` | Reads `self.state.processing or self.state.capturing` |

### `UtteranceCapturer`

| v10 | v11 |
|---|---|
| Owns `self.capturing`, `self.processing` booleans | Reads `self.state.capturing`, `self.state.processing` |
| Has `self._vad_weak` weakref | No processor refs at all |
| `start_capture()` method (called by OWW directly) | `clear_chunks()` method (called by `state.request_capture()`) |
| `finally: self.processing = False; vad.reset_vad()` | `finally: self.state.set_phase("idle")` |
| `GATE_VAD_ALL` branch: `self.processing = False` | `GATE_VAD_ALL` branch: `self.state.set_phase("idle")` |
| `GATE_VAD_CREATE_TASK` branch: `self.processing = False` | `GATE_VAD_CREATE_TASK` branch: `self.state.set_phase("idle")` |
| On VAD stop-speaking: `self.processing = True` (raw mutation) | On VAD stop-speaking: `self.state.set_phase("processing")` |

### `main()`

```python
# v10
capturer = UtteranceCapturer()
vad_processor = GatedVADProcessor(vad_analyzer=..., capturer=capturer)
capturer._vad_weak = weakref.ref(vad_processor)   # manual weakref wiring
wake_processor = OpenWakeWordProcessor(capturer)

# v11
state = PipelineState()
capturer = UtteranceCapturer(state=state)
vad_processor = GatedVADProcessor(vad_analyzer=..., state=state)
wake_processor = OpenWakeWordProcessor(state=state)
state.set_transport(input_transport)   # wiring after all objects exist
state.set_vad(vad_processor)
state.set_capturer(capturer)
```

---

## What was also fixed in v11 (not strictly state breakout)

- **CancelFrame/EndFrame crash fix** (selective VAD conditional): `GatedVADProcessor.process_frame` now only routes `StartFrame` and `AudioRawFrame`/`InputAudioRawFrame` to the VAD controller. In v10, the condition was `elif isinstance(frame, AudioRawFrame...)` — other frame types fell through into further branches depending on gate settings, which could reach `_vad_controller.process_frame` with a CancelFrame and crash on exit. v11 makes the conditional explicitly exhaustive and exclusive.
- **Stream pause during processing**: v10 had no stream management beyond shutdown patching. v11 pauses the PyAudio stream for the duration of `_cognitive_loop` to prevent USB audio buffer overflow during the CPU-heavy Claude subprocess.
