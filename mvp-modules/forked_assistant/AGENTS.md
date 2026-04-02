# forked_assistant — Agent Context

## What This Is

Active development of a two-process voice assistant architecture for Raspberry Pi 4. The recorder child (pinned to core 0) owns the microphone and runs the Pipecat pipeline. The master process (cores 1–3) runs the cognitive loop (STT → Claude → response handling). They communicate via SharedMemory (audio data) and Pipe (control signals).

This design solves the single-process shutdown crash documented in `archive/step7/`.

## Start Here

**`spec/implementation_framework.md`** is the guiding document for this effort. It defines the effort unit (EU) phasing, dependency graph, session principles, reference code pointers, and the file layout roadmap. Read it before starting any implementation session. The other specs (`architecture.md`, `interface_spec.md`, `recorder_state_spec.md`, `stub_contracts.md`) contain the technical design — the framework doc tells you how to approach the work.

## Directory Layout

```
forked_assistant/
├── AGENTS.md          ← you are here
├── requirements.txt   ← Pi-authoritative version pins (see file for platform notes)
├── spec/              ← design specifications (read before coding)
│   ├── architecture.md            ← high-level two-process design
│   ├── interface_spec.md          ← ring buffer layout, pipe message shapes
│   ├── recorder_state_spec.md     ← recorder state machine: DORMANT/WAKE_LISTEN/CAPTURE
│   ├── stub_contracts.md          ← EU-3 parallel tracks: stub vs real IPC
│   └── implementation_framework.md ← EU phasing, session principles, dependency graph (READ FIRST)
├── src/               ← library code
│   ├── ring_buffer.py             ← SharedMemory ring: writer/reader, header format
│   ├── recorder_state.py         ← RecorderState base class: phase logic, processor hooks
│   └── recorder_child.py         ← EU-3d: merged recorder subprocess (RecorderChild + Pipecat pipeline)
├── test/              ← harnesses and smoke tests
│   ├── smoke_test_shm.py         ← EU-1+EU-2: SharedMemory and ring buffer IPC tests
│   ├── track1_ipc_harness.py     ← EU-3b: fork + real SHM/pipe, FakeAudioDriver
│   ├── track2_pipeline_harness.py ← EU-3c: single-process Pipecat + real mic/ONNX + stub IPC
│   └── test_harness.py            ← EU-3d: master-side harness (spawns real recorder child)
└── archive/           ← superseded snapshots
    ├── 2026-04-02T1400_track2_pipeline_harness.py ← v01 baseline (no ring write simulation)
    └── 2026-04-02T1401_track2_pipeline_harness.py ← v03 dead end (per-processor timing, wrong instrumentation unit)
```

## Implementation Phasing (Effort Units)

| EU | Description | Status |
|----|-------------|--------|
| EU-1 | SharedMemory smoke test | Complete (`test/smoke_test_shm.py`) |
| EU-2 | Ring buffer module | Complete (`src/ring_buffer.py`, tested in smoke_test) |
| EU-3a | RecorderState base class | Complete (`src/recorder_state.py`) |
| EU-3b | Track 1: IPC harness (fork + real SHM/pipe, no Pipecat) | Complete (`test/track1_ipc_harness.py`) |
| EU-3c | Track 2: Pipeline harness (real Pipecat + stub IPC) | Complete (`test/track2_pipeline_harness.py`) |
| EU-3d | Merge: Track 1 + Track 2 into real recorder child | Code complete — needs Pi run (`src/recorder_child.py`, `test/test_harness.py`) |

EU-3b and EU-3c are **parallel tracks** that can be developed independently. EU-3d merges them.

## Hard Constraints (from step 7 crash analysis)

These are proven failure modes. Do not relax them:

1. **Stream ops (stop/start) must be async tasks**, never synchronous from Pipecat callbacks → PortAudio deadlock / USB fault
2. **OWW full 5-buffer reset on every ungate transition** → false-positive wake detections without it
3. **Silero LSTM reset before first frame of new capture** → stale hidden states contaminate utterance
4. **No concurrent ONNX workloads** → OWW and Silero in non-overlapping phases only
5. **Every FrameProcessor subclass must override `process_frame`** and call both `super().process_frame()` and `push_frame()` → silent frame swallowing otherwise

## Import Convention

Test files add `sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))` to resolve `src/` imports. This supports direct execution: `python test/smoke_test_shm.py` from the `forked_assistant/` directory.

## What's Next

### EU-3d — code complete, needs Pi run

`src/recorder_child.py` and `test/test_harness.py` are code-complete. The merge combines Track 1's real downstream port (ring buffer + pipe signals) with Track 2's proven Pipecat pipeline into a single forked recorder child.

Run with ReSpeaker:
```
cd ~/raspberry-ai/mvp-modules/forked_assistant
source ~/pipecat-agent/venv/bin/activate
python test/test_harness.py
```

Expected: 3 wake→capture→VAD cycles, ring spans readable by master with correct byte counts, clean exit. Also test Ctrl+C from at least one active state — shutdown must be clean (no hang, no Pi reboot).

**Merge checklist verified:**
1. `RecorderChild.__init__` passes real pipe, RingBufferWriter owns SharedMemory directly
2. All signal dicts match `interface_spec.md` exactly (WAKE_DETECTED, VAD_STARTED, VAD_STOPPED, STATE_CHANGED, READY)
3. `write_audio()` delegates to `ring_buffer.RingBufferWriter.write()`
4. `write_pos` included in all signal payloads

**Architecture notes for Pi run:**
- READY is sent before `runner.run()`. The first dormant→wake_listen `_start_stream()` may skip (stream not yet initialized by Pipecat); this is harmless because Pipecat starts the stream itself on pipeline start. Subsequent dormant transitions use `_start_stream()`/`_stop_stream()` normally.
- Both master and child handle SIGINT. The master sends SHUTDOWN over the pipe; the child also has a direct signal handler that cancels the pipeline task.
- The child timeout on join is 5s (up from Track 1's 3s) to allow for PyAudio + ONNX teardown.

### EU-3b — complete (2026-04-02)

Track 1 IPC harness (`test/track1_ipc_harness.py`) passed on Pi. SharedMemory, Pipe, fork, core pinning, and shutdown all validated with synthetic audio.

### EU-3c — complete (2026-04-02)

Track 2 pipeline harness (`test/track2_pipeline_harness.py`) is complete. All spec criteria proven on Pi including async OWW predict. Duty cycle: 6% budget utilization, 0 frames over 20ms budget.

### EU-4 — ready to start once EU-3d Pi run passes

Master process with batch-mode utterance processing (STT + Claude). See `spec/implementation_framework.md` — EU-4.
