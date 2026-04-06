# Stub Contracts: EU-3 Parallel Tracks

**Date:** 2026-04-01
**Status:** Design spec
**Parent:** `forked_assistant/implementation_framework.md`

---

## Purpose

EU-3 is split into two parallel tracks (EU-3b and EU-3c) that are developed independently and merged in EU-3d. This document specifies the exact Python-level boundary between the tracks so that the merge is substitution, not reconciliation.

---

## The Seam: RecorderState

`RecorderState` is the merge boundary. It has two ports:

**Upstream port** — called by Pipecat processors inside the recorder child:
- Phase reads (`phase`, `dormant`, `wake_listen`, `capture` properties)
- `write_audio()` — frame data into ring buffer
- Signal emission methods — fired by OWW/Silero event handlers

**Downstream port** — crosses the process boundary:
- `write_audio()` → SharedMemory ring buffer write
- Signal emission methods → `pipe.send()` calls
- Command receipt → `pipe.recv()` in command listener loop
- Stream lifecycle → PyAudio `start_stream()` / `stop_stream()`

**Track 2 (Pipecat-first)** tests the upstream port with real processors. It stubs the downstream port with `RecorderStateStub` — a subclass that collects signals into a list and discards ring buffer writes.

**Track 1 (IPC-first)** tests the downstream port with real SharedMemory, real Pipe, real fork. It stubs the upstream port with `FakeAudioDriver` — a coroutine that fires fake frames and fake events on timers.

**The merge (EU-3d):** drop `RecorderState` (real downstream port) into Track 2's working Pipecat pipeline. `RecorderStateStub` is replaced; `FakeAudioDriver` is removed. No other changes needed.

---

## Shared Method Signatures

Both tracks must agree on these signatures. Neither track may alter them.

### Phase read properties (read-only, no side effects)

```python
state.phase       → str    # "dormant" | "wake_listen" | "capture"
state.dormant     → bool
state.wake_listen → bool
state.capture     → bool
```

### Phase transition (shared logic, side effects fire on transition)

```python
await state.set_phase(new_phase: str) → None
```

Side effects are defined in `recorder_state_spec.md`. Track 2 tests these with real stream ops. Track 1 tests that `set_phase()` emits `STATE_CHANGED` over the pipe (stream ops are no-ops in Track 1).

### Signal emission methods (upstream port calls → downstream port sends)

```python
state.signal_wake_detected(score: float, keyword: str) → None
state.signal_vad_started() → None
state.signal_vad_stopped() → None
state.signal_state_changed() → None   # called internally by set_phase()
```

These are called from Pipecat processor event handlers. They are synchronous (no await) — they must not block.

### Audio write

```python
state.write_audio(frame_bytes: bytes) → None
```

Called from `AudioShmRingWriteProcessor.process_frame` on every `AudioRawFrame` when not dormant. Synchronous. Must complete in under 1ms (it is called on the asyncio event loop during frame processing).

### Frame counters

```python
state.inc_vad_frames() → None      # called by GatedVADProcessor per frame fed to Silero
state.inc_total_frames() → None    # called by GatedVADProcessor per audio frame seen
state.vad_frame_count   → int
state.total_frame_count → int
```

### Write position (for signal payloads)

```python
state.write_pos → int    # current monotonic byte offset, read when building signal dicts
```

---

## Track 1 Spec: IPC/Buffer with Stubbed Audio

**File:** `forked_assistant/track1_ipc_harness.py`
**Scope:** Real fork, real SharedMemory, real Pipe, real core pinning, real shutdown. No Pipecat, no PyAudio, no OWW, no Silero.

### RecorderState (real downstream port)

Track 1 uses a `RecorderState` whose downstream port is fully real:

- `write_audio()` → memcpy into SharedMemory ring buffer, advance `write_pos`
- `signal_*()` methods → `pipe.send(dict)`
- `set_phase()` → real state transitions, real `signal_state_changed()`, but `start_stream()` / `stop_stream()` are no-ops (no PyAudio)
- Command receipt: real `pipe.recv()` in command listener loop

### FakeAudioDriver

Replaces the Pipecat pipeline inside the child process. Runs as an asyncio task alongside the command listener.

```python
async def fake_audio_driver(state: RecorderState,
                             wake_delay_secs: float = 4.0,
                             speech_duration_secs: float = 2.5,
                             silence_after_secs: float = 1.0):
    """
    Simulates PyAudio frame production and OWW/Silero events.
    Runs continuously; each 'utterance' cycle repeats after completion.
    """
    frame = bytes(FRAME_BYTES)          # 640 zero-bytes per 20ms frame
    
    while True:
        # --- WAKE_LISTEN phase: emit frames + fire wake word after delay ---
        wake_deadline = asyncio.get_event_loop().time() + wake_delay_secs
        while asyncio.get_event_loop().time() < wake_deadline:
            if state.wake_listen:
                state.write_audio(frame)
                state.inc_total_frames()
            await asyncio.sleep(FRAME_DURATION)  # 0.020s
        
        if state.wake_listen:
            state.signal_wake_detected(score=0.75, keyword="hey_jarvis")
        
        # --- Wait for SET_CAPTURE command (command listener will call set_phase) ---
        # Poll until state transitions (command listener runs concurrently)
        for _ in range(50):   # up to 1s
            if state.capture:
                break
            await asyncio.sleep(0.020)
        
        # --- CAPTURE phase: emit frames + fire VAD_STARTED + VAD_STOPPED ---
        if state.capture:
            state.signal_vad_started()
            speech_deadline = asyncio.get_event_loop().time() + speech_duration_secs
            while asyncio.get_event_loop().time() < speech_deadline:
                state.write_audio(frame)
                state.inc_total_frames()
                state.inc_vad_frames()
                await asyncio.sleep(FRAME_DURATION)
            
            await asyncio.sleep(silence_after_secs)
            state.signal_vad_stopped()
        
        # --- Back to WAKE_LISTEN phase (command listener handles SET_WAKE_LISTEN) ---
        for _ in range(50):
            if state.wake_listen:
                break
            await asyncio.sleep(0.020)
```

**Key properties of the fake driver:**
- Emits frames at the real 20ms cadence (not faster — this stresses the ring buffer at realistic write rate)
- `wake_delay_secs`, `speech_duration_secs`, `silence_after_secs` are configurable at construction time
- Does NOT call `set_phase()` — that is the command listener's job (responding to master commands)
- Does NOT start/stop PyAudio — `set_phase()` stream ops are no-ops in Track 1

### Track 1 test harness (master side)

The master side is minimal. It is the prototype of EU-4's master:

```
1. Create SharedMemory (SHM_NAME, SHM_SIZE)
2. Create Pipe (duplex=True)
3. Spawn child process running recorder_child_track1(pipe_child_end, SHM_NAME)
4. Wait for READY
5. Send SET_WAKE_LISTEN
6. On WAKE_DETECTED {write_pos: W}:
     print wake event + score
     Send SET_CAPTURE
7. On VAD_STARTED {write_pos: S}:
     note start_pos = S
8. On VAD_STOPPED {write_pos: E}:
     audio = ring_reader.read(start_pos, E)
     print f"Captured {len(audio)} bytes ({len(audio)/32000:.2f}s)"
     Send SET_WAKE_LISTEN
9. Repeat 3 cycles
10. Send SHUTDOWN, child.join(timeout=3)
11. shm.unlink()
```

### What Track 1 proves

- SharedMemory cross-process: named segment created by master, opened by child, written by child, read by master
- Ring buffer: correct wrap-around behavior, correct span reads, `write_pos` monotonically advancing
- Pipe bidirectional: commands flow master→child, signals flow child→master, no deadlock
- Fork + core pinning: `multiprocessing.Process` spawns correctly, `os.sched_setaffinity(0, {0})` works on Pi ARM64
- State transitions via pipe commands: command listener calls `set_phase()`, `STATE_CHANGED` arrives at master
- Write rate stress: 20ms cadence over a 4s wake cycle = 200 frames = 128KB written before the span is read — exercises ring adequately
- Shutdown from all states: send SHUTDOWN mid-wake-listen, mid-capture, after completion

---

## Track 2 Spec: Pipecat Pipeline with Stubbed IPC

**File:** `forked_assistant/track2_pipeline_harness.py`
**Scope:** Real Pipecat, real OWW, real Silero, real PyAudio, real RecorderState state machine. No fork, no SharedMemory, no Pipe. Single process.

### RecorderStateStub

Subclasses `RecorderState`. Overrides the downstream port methods. All other methods (state machine, stream ops, phase properties, OWW/Silero resets) are inherited and real.

```python
class RecorderStateStub(RecorderState):
    """
    RecorderState with real state machine but stubbed IPC.
    Signals are collected into self.events for inspection.
    Ring buffer writes are discarded.
    Construct without pipe or SharedMemory arguments.
    """
    
    def __init__(self):
        super().__init__(pipe=None, shm=None)
        self.events: list[dict] = []
    
    # --- Downstream port overrides ---
    
    def write_audio(self, frame_bytes: bytes) -> None:
        pass  # discard — ring buffer not under test in Track 2
    
    def signal_wake_detected(self, score: float, keyword: str) -> None:
        self.events.append({
            "cmd": "WAKE_DETECTED",
            "write_pos": self.write_pos,
            "score": score,
            "keyword": keyword,
        })
        print(f"  [STUB] WAKE_DETECTED score={score:.3f} keyword={keyword}")
    
    def signal_vad_started(self) -> None:
        self.events.append({"cmd": "VAD_STARTED", "write_pos": self.write_pos})
        print(f"  [STUB] VAD_STARTED write_pos={self.write_pos}")
    
    def signal_vad_stopped(self) -> None:
        self.events.append({"cmd": "VAD_STOPPED", "write_pos": self.write_pos})
        print(f"  [STUB] VAD_STOPPED write_pos={self.write_pos}")
    
    def signal_state_changed(self) -> None:
        self.events.append({"cmd": "STATE_CHANGED", "state": self.phase})
        print(f"  [STUB] STATE_CHANGED → {self.phase}")
```

### Track 2 Pipecat pipeline

Identical structure to `recorder_child.py` as it will exist post-merge, except:
- Uses `RecorderStateStub` instead of `RecorderState`
- No fork, no SharedMemory, no Pipe setup
- Command dispatch: `state.set_phase()` called directly from the test harness (no pipe)

```python
async def main():
    state = RecorderStateStub()
    
    transport = LocalAudioTransport(...)
    vad_processor = GatedVADProcessor(vad_analyzer=SileroVADAnalyzer(...), state=state)
    wake_processor = OpenWakeWordProcessor(state=state)
    ring_writer = AudioShmRingWriteProcessor(state=state)
    
    state.set_transport(transport.input())
    state.set_vad(vad_processor)
    
    pipeline = Pipeline([transport.input(), vad_processor, wake_processor, ring_writer])
    
    # Direct command dispatch — no pipe
    runner = PipelineRunner()
    task = PipelineTask(pipeline)
    
    asyncio.create_task(direct_command_driver(state))
    await runner.run(task)

async def direct_command_driver(state: RecorderStateStub):
    """Drives state transitions without a pipe — simulates master commands."""
    await asyncio.sleep(1.0)
    await state.set_phase("wake_listen")   # arm OWW
    # From here, state transitions fire automatically:
    # OWW → signal_wake_detected → (harness observes events list and calls set_phase("capture"))
    # Silero → signal_vad_stopped → (harness observes and calls set_phase("wake_listen"))
```

For Track 2 the harness monitors `state.events` and drives transitions in response:

```python
async def direct_command_driver(state: RecorderStateStub):
    await asyncio.sleep(1.0)
    await state.set_phase("wake_listen")
    
    while True:
        await asyncio.sleep(0.1)
        if state.events and state.events[-1]["cmd"] == "WAKE_DETECTED":
            await state.set_phase("capture")
        elif state.events and state.events[-1]["cmd"] == "VAD_STOPPED":
            print(f"  [HARNESS] VAD_STOPPED — {len(state.events)} events total")
            await state.set_phase("wake_listen")
```

### What Track 2 proves

- Pipecat pipeline survives inside future forked child (single-process is sufficient for logic correctness)
- `GatedVADProcessor` (adapted from v10a) works with `RecorderState` as its state interface
- `OpenWakeWordProcessor` (adapted from v10a) works with `RecorderState`
- `AudioShmRingWriteProcessor` calls `write_audio()` on every audio frame when not dormant (observed via stub counter if desired)
- State machine transitions fire correct side effects: OWW full reset on CAPTURE→WAKE_LISTEN, Silero reset on →CAPTURE
- VAD event handlers emit correct signals: `signal_vad_started`, `signal_vad_stopped`
- OWW detection emits `signal_wake_detected`
- `set_phase()` always emits `signal_state_changed`
- Stream ops (start/stop PyAudio) sequence correctly without deadlock
- Ctrl+C exits cleanly (single process, the original easy case)

---

## EU-3d Merge Contract

The merge session replaces `RecorderStateStub` with `RecorderState` in Track 2's pipeline. The merge checklist:

1. `RecorderState.__init__` accepts `pipe: Connection` and `shm: SharedMemory` (passed in by master after fork)
2. `RecorderState` constructor signature is compatible: `RecorderStateStub.__init__` calls `super().__init__(pipe=None, shm=None)` — verify real `RecorderState` handles `None` gracefully during construction (set at `state.set_pipe()` / `state.set_shm()` after fork, before READY)
3. All signal emission methods in real `RecorderState` produce the exact dict formats specified in `interface_spec.md`
4. `write_audio()` in real `RecorderState` calls `audio_shm_ring.AudioShmRingWriter.write(frame_bytes)` — uses EU-2 module
5. `state.write_pos` returns the ring buffer's current `write_pos` — included in signal dicts

Merge is complete when Track 2's full Pipecat pipeline runs with real `RecorderState`, inside a forked child, with a Track-1-style master reading the ring buffer and printing captured spans. That is the `test_harness.py` target.

---

## SharedMemory Position: Track 2 Does Not Use It

Track 2 stubs `write_audio()` to a no-op. `state.write_pos` increments (monotonically) but nothing is written to any buffer. The ring buffer is entirely Track 1's concern. There is no in-process SharedMemory in Track 2.

This is intentional. The ring buffer module (`assistant/audio_shm_ring.py` from EU-2) is imported by Track 1 and by the merged `assistant/recorder_process.py`. Track 2 imports neither.
