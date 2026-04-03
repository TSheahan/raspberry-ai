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
│   ├── recorder_child.py         ← EU-3d: merged recorder subprocess (RecorderChild + Pipecat pipeline)
│   └── master.py                 ← EU-4: master process — batch-mode cognitive loop
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
| EU-3d | Merge: Track 1 + Track 2 into real recorder child | Complete (`src/recorder_child.py`, `test/test_harness.py`) |
| EU-4 | Master process — batch mode (STT + Claude) | Complete (`src/master.py`, proven 2026-04-03) |

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

### EU-5 — streaming STT extension (future)

Batch-mode EU-4 is proven. EU-5 adds streaming STT: on WAKE_DETECTED, opens a Deepgram live WebSocket and tails the ring buffer, sending chunks as they arrive. Uses a configurable termination policy (VAD_STOPPED after N seconds silence, explicit command, or timeout). The ring buffer + signal design was chosen to enable this without changing the recorder child. See `spec/implementation_framework.md` EU-5 for scope.

### Stability validation — multi-turn (pending)

EU-4 has one confirmed successful voice turn (2026-04-03) with clean shutdown. The success criteria require 3–5 consecutive turns without degradation. This has not been run yet. Before EU-5, confirm:
- 3+ consecutive wake→capture→STT→Claude→wake_listen cycles without drift
- Clean Ctrl+C from CAPTURE state (current test was from WAKE_LISTEN)
- No queue depth alarms over extended runtime

## Completed Effort Units

### EU-4 — complete (2026-04-03)

Master process batch mode (`src/master.py`) proven on Pi across three runs:

- **Run 1:** Audio driver buffer overrun, transcription failed. Root cause: capture span started at VAD_STARTED, discarding utterance onset.
- **Run 2:** Span start fixed to wake_pos. Transcription succeeded ("Hello?", 1.71s latency). Pi rebooted on Ctrl+C — SIGINT race: child called `task.cancel()` directly, bypassing `stop_stream()`.
- **Run 3:** Two-phase shutdown protocol implemented. Voice turn complete (score 0.879, 3.12s, "Hello.", 1.82s STT). Clean Ctrl+C shutdown: SHUTDOWN_COMMENCED → stream stopped → pipeline drained → SHUTDOWN_FINISHED → `[master] done`. No reboot. 0/485 frames over budget.

Key changes to `recorder_child.py`: `_initiate_shutdown()` once-only coroutine, SIGINT/SIGTERM/SHUTDOWN all converge to it. Key changes to `master.py`: `shutdown_child()` waits for SHUTDOWN_FINISHED, `master_loop` returns on SHUTDOWN_COMMENCED, `shutdown_child` runs in `finally` on all exit paths. Protocol documented in `interface_spec.md` §3 and `memory/shutdown_and_buffer_patterns.md` Root Cause 4.

### EU-3d — complete (2026-04-02)

Merged recorder child passed on Pi: 2 full wake→capture→VAD cycles with correct ring spans (61440 / 59520 bytes, ~1.9s each), Ctrl+C mid-wake-listen on 3rd cycle with clean shutdown. All success criteria met.

### EU-3b — complete (2026-04-02)

Track 1 IPC harness (`test/track1_ipc_harness.py`) passed on Pi. SharedMemory, Pipe, fork, core pinning, and shutdown all validated with synthetic audio.

### EU-3c — complete (2026-04-02)

Track 2 pipeline harness (`test/track2_pipeline_harness.py`) passed on Pi including async OWW predict. Duty cycle: 6% budget utilization, 0 frames over 20ms budget.
