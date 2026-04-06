# Recorder Child State Machine

**Date:** 2026-04-01
**Status:** Design spec
**Parent:** `forked_assistant/architecture.md`

---

## Overview

The recorder child has three operating states controlled by the master process via pipe commands. State transitions are managed by a centralized `RecorderState` object following the pattern established in v11's `PipelineState`.

---

## States

### DORMANT

- PyAudio stream: **stopped**
- Ring buffer writes: **stopped**
- OWW inference: **not running**
- Silero VAD inference: **not running**
- Audio frames reaching pipeline processors: **none**

This is the initial state after startup (before first command) and the state entered on `SET_DORMANT`. The child is alive but idle. PyAudio stream is stopped to prevent any USB audio activity.

### WAKE_LISTEN

- PyAudio stream: **active**
- Ring buffer writes: **active** (all audio frames written)
- OWW inference: **running** (processing frames for wake word detection)
- Silero VAD inference: **not running** (frames not forwarded to VAD controller)
- Signals emitted: `WAKE_DETECTED` on wake word detection

This is the normal listening state. Audio flows through the pipeline. OWW processes frames and signals the master if a wake word is detected. The master decides whether to transition to CAPTURE.

### CAPTURE

- PyAudio stream: **active**
- Ring buffer writes: **active** (all audio frames written)
- OWW inference: **not running** (gated, buffers cleared)
- Silero VAD inference: **running** (frames forwarded to VAD controller)
- Signals emitted: `VAD_STARTED` on speech onset, `VAD_STOPPED` on speech offset

This state is entered when the master sends `SET_CAPTURE` (typically in response to `WAKE_DETECTED`). Silero VAD runs to detect speech boundaries. The master reads the ring buffer between the reported positions to extract the utterance.

---

## Transition Table

| From | To | Trigger | Side effects |
|------|-----|---------|-------------|
| DORMANT | WAKE_LISTEN | `SET_WAKE_LISTEN` | Start PyAudio stream. Reset OWW (full 5-buffer reset). Begin ring buffer writes. |
| DORMANT | CAPTURE | `SET_CAPTURE` | Start PyAudio stream. Reset Silero LSTM states. Begin ring buffer writes. |
| WAKE_LISTEN | DORMANT | `SET_DORMANT` | Stop PyAudio stream. Stop ring buffer writes. |
| WAKE_LISTEN | CAPTURE | `SET_CAPTURE` | Stop OWW (clear chunks). Reset Silero LSTM states. Reset VAD frame count. (Stream stays active, ring writes continue.) |
| CAPTURE | DORMANT | `SET_DORMANT` | Stop PyAudio stream. Stop ring buffer writes. |
| CAPTURE | WAKE_LISTEN | `SET_WAKE_LISTEN` | Stop Silero (stop forwarding frames). Reset OWW (full 5-buffer reset). Reset VAD frame count. (Stream stays active, ring writes continue.) |
| CAPTURE | CAPTURE | `SET_CAPTURE` | No-op (or re-reset Silero if desired). |
| WAKE_LISTEN | WAKE_LISTEN | `SET_WAKE_LISTEN` | No-op (or re-reset OWW if desired). |

Every transition emits `STATE_CHANGED {state: "..."}` back to the master over the pipe.

### Transition ordering constraints

These ordering rules come from step 7 crash analysis and must be preserved:

1. **Silero LSTM reset before stream resume.** When entering CAPTURE from DORMANT, Silero states must be reset before the first audio frame reaches the VAD controller. Since `set_phase` runs side-effects before enabling the processing gate, this is satisfied.

2. **OWW full reset on ungate.** When entering WAKE_LISTEN from CAPTURE (or DORMANT), all five OWW preprocessor buffers must be cleared before the first audio frame reaches OWW predict. This prevents stale-feature false positives.

3. **Stream ops never called synchronously from Pipecat frame callbacks.** `stop_stream()` / `start_stream()` must be scheduled as async tasks, not called within `process_frame`. (PortAudio deadlock / USB fault on Pi.)

---

## RecorderState Object

Follows the v11 `PipelineState` pattern: centralized state, read-only property exposure, side-effects on transition, weak-ref pointers into controlled objects.

### Fields

```python
class RecorderState:
    _phase: str                     # "dormant" | "wake_listen" | "capture"
    _write_pos: int                 # monotonic byte counter for ring buffer
    _vad_frame_count: int           # frames fed to Silero this capture session
    _total_frame_count: int         # all audio frames since process start
    
    # Controlled object references
    _vad_ref: weakref               # → GatedVADProcessor
    _oww_ref: weakref               # → OpenWakeWordProcessor
    _transport_ref: object          # → input_transport (strong ref, long-lived)
    _ring_writer_ref: weakref       # → AudioShmRingWriteProcessor
    _pipe: Connection               # pipe back to master (strong ref)
    _shm: SharedMemory              # shared memory segment (strong ref)
```

### Read-only properties

```python
state.phase          → str
state.dormant        → bool  (phase == "dormant")
state.wake_listen    → bool  (phase == "wake_listen")
state.capture        → bool  (phase == "capture")
state.write_pos      → int
state.vad_frame_count → int
state.total_frame_count → int
```

### Phase transition method

```python
async def set_phase(self, new_phase: str):
    old_phase = self._phase
    if old_phase == new_phase:
        self._signal_state_changed()
        return
    
    # Exit side-effects
    if old_phase == "wake_listen":
        self._clear_oww()               # clear OWW chunks
    elif old_phase == "capture":
        pass                             # no cleanup needed
    elif old_phase == "dormant":
        await self._start_stream()       # scheduled as task
    
    # Entry side-effects
    if new_phase == "wake_listen":
        self._reset_oww_full()           # 5-buffer reset
        self._vad_frame_count = 0
    elif new_phase == "capture":
        await self._reset_silero()       # LSTM hidden states
        self._vad_frame_count = 0
    elif new_phase == "dormant":
        await self._stop_stream()        # scheduled as task
    
    self._phase = new_phase
    self._signal_state_changed()
```

Note: stream start/stop are `await`ed tasks (not synchronous calls) per the PortAudio constraint.

### Signal emission

```python
def _signal_state_changed(self):
    self._pipe.send({"cmd": "STATE_CHANGED", "state": self._phase})

def signal_wake_detected(self, score: float, keyword: str):
    self._pipe.send({
        "cmd": "WAKE_DETECTED",
        "write_pos": self._write_pos,
        "score": score,
        "keyword": keyword,
    })

def signal_vad_started(self):
    self._pipe.send({"cmd": "VAD_STARTED", "write_pos": self._write_pos})

def signal_vad_stopped(self):
    self._pipe.send({"cmd": "VAD_STOPPED", "write_pos": self._write_pos})
```

---

## Pipecat Pipeline (Recorder Child)

```
transport.input()
    → GatedVADProcessor      # Silero ONNX — only in CAPTURE state
    → OpenWakeWordProcessor   # OWW ONNX — only in WAKE_LISTEN state
    → AudioShmRingWriteProcessor   # writes all audio frames to shared memory
```

### AudioShmRingWriteProcessor (new processor)

Replaces `UtteranceCapturer` in the recorder child. Minimal responsibility:

```python
class AudioShmRingWriteProcessor(FrameProcessor):
    """Writes audio frames to the shared ring buffer via RecorderState."""
    
    def __init__(self, state: RecorderState):
        super().__init__()
        self.state = state
    
    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        
        if isinstance(frame, AudioRawFrame) and not self.state.dormant:
            self.state.write_audio(frame.audio)
        
        await self.push_frame(frame, direction)
```

The `state.write_audio()` method handles the ring buffer memcpy and write_pos advancement.

### Processor modifications from v10a

**GatedVADProcessor:** Change `self._capturer.capturing` reads to `self.state.capture`. On `on_speech_started` / `on_speech_stopped`, call `self.state.signal_vad_started()` / `self.state.signal_vad_stopped()` instead of broadcasting Pipecat frames across the process boundary. The VAD processor no longer needs to emit `VADUserStoppedSpeakingFrame` — that was an intra-pipeline signal consumed by UtteranceCapturer, which no longer exists in the recorder child.

**OpenWakeWordProcessor:** Change `self.capturer.processing or self.capturer.capturing` to `not self.state.wake_listen` (gate when not in wake_listen state). On wake detection, call `self.state.signal_wake_detected(score, wakeword)` instead of `self.capturer.start_capture()`.

---

## Command Dispatch Loop (Recorder Child Main)

The recorder child runs two concurrent concerns:
1. Pipecat pipeline (driven by PyAudio callbacks → asyncio event loop)
2. Pipe command listener

```python
async def command_listener(state: RecorderState, pipe: Connection):
    """Listen for commands from master, dispatch state transitions."""
    loop = asyncio.get_running_loop()
    
    while True:
        # Wait for pipe to be readable (non-blocking integration with asyncio)
        msg = await loop.run_in_executor(None, pipe.recv)
        
        cmd = msg["cmd"]
        if cmd == "SET_DORMANT":
            await state.set_phase("dormant")
        elif cmd == "SET_WAKE_LISTEN":
            await state.set_phase("wake_listen")
        elif cmd == "SET_CAPTURE":
            await state.set_phase("capture")
        elif cmd == "SHUTDOWN":
            await state.set_phase("dormant")
            break
    
    # After SHUTDOWN: clean up Pipecat, PyAudio, shared memory
    await cleanup(state)
```

This runs as an `asyncio.create_task` alongside the Pipecat runner.
