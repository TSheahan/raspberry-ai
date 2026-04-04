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
│   ├── agent_session_spec.md      ← EU-6: AgentSession abstract interface + CursorAgentSession contract
│   └── implementation_framework.md ← EU phasing, session principles, dependency graph (READ FIRST)
├── src/               ← library code
│   ├── ring_buffer.py             ← SharedMemory ring: writer/reader, header format
│   ├── recorder_state.py         ← RecorderState base class: phase logic, processor hooks
│   ├── recorder_child.py         ← EU-3d: merged recorder subprocess (RecorderChild + Pipecat pipeline)
│   ├── agent_session.py          ← EU-6: AgentSession base + CursorAgentSession implementation
│   └── master.py                 ← EU-4: master process — batch-mode cognitive loop
├── test/              ← harnesses and smoke tests
│   ├── smoke_test_shm.py         ← EU-1+EU-2: SharedMemory and ring buffer IPC tests
│   ├── track1_ipc_harness.py     ← EU-3b: fork + real SHM/pipe, FakeAudioDriver
│   ├── track2_pipeline_harness.py ← EU-3c: single-process Pipecat + real mic/ONNX + stub IPC
│   └── test_harness.py            ← EU-3d: master-side harness (spawns real recorder child)
└── archive/           ← superseded snapshots
    ├── 2026-04-02T1400_track2_pipeline_harness.py ← v01 baseline (no ring write simulation)
    ├── 2026-04-02T1401_track2_pipeline_harness.py ← v03 dead end (per-processor timing, wrong instrumentation unit)
    ├── 2026-04-04_streaming_architecture_analysis.md ← EU-5 design brief: streaming vs batch STT analysis
    ├── 2026-04-04_wrapped_cursor_agent.py           ← Cursor CLI pre-spawn smoke test (EU-6 reference)
    └── 2026-04-04_wrapped_cursor_agent_context.md   ← Cursor CLI invocation pattern, stream-json schema, findings
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
| EU-5 | Streaming STT — Deepgram live WebSocket + ring buffer tail | Code written (`src/master.py`); Pi validation pending |
| EU-6 | Agent module — `AgentSession` abstraction + `CursorAgentSession` (Cursor CLI) | Code written (`src/agent_session.py`, integrated in `src/master.py`); Pi validation pending |

EU-3b and EU-3c are **parallel tracks** that can be developed independently. EU-3d merges them.

## Hard Constraints (from step 7 crash analysis)

These are proven failure modes. Do not relax them:

1. **Stream ops (stop/start) must be async tasks**, never synchronous from Pipecat callbacks → PortAudio deadlock / USB fault
2. **OWW full 5-buffer reset on every ungate transition** → false-positive wake detections without it
3. **Silero LSTM reset before first frame of new capture** → stale hidden states contaminate utterance
4. **No concurrent ONNX workloads** → OWW and Silero in non-overlapping phases only
5. **Every FrameProcessor subclass must override `process_frame`** and call both `super().process_frame()` and `push_frame()` → silent frame swallowing otherwise

## Agent Subprocess — Privilege Separation

The Cursor CLI subprocess runs as a dedicated `agent` Linux user via `sudo -u agent -H`. The voice assistant processes (`master.py`, recorder child) run as `user`. The sudoers entry is narrow: `user ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent`.

Set `AGENT_USER=agent` in `.env` to enable. Leave unset for dev/local runs (subprocess inherits current user). `AGENT_BIN` and `AGENT_WORKSPACE` must point to `agent`'s home when `AGENT_USER` is active.

Pi provisioning steps: `profiling-pi/agent-user-setup.md`. Design rationale: `archive/2026-04-04_privilege_separation_analysis.md`.

## Import Convention

Test files add `sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))` to resolve `src/` imports. This supports direct execution: `python test/smoke_test_shm.py` from the `forked_assistant/` directory.

## What's Next

### EU-4 validation (rolled into EU-5 Pi session)

The remaining EU-4 success criteria (agent response on a full turn, 3–5 consecutive cycles, Ctrl+C from CAPTURE) are folded into the EU-5 Pi validation session below. The batch STT and `stub_claude()` / `run_claude()` paths are removed from `master.py`; the EU-5 streaming path is the delivery artifact for step 7.

### EU-5 + EU-6 — code written; Pi validation is the next session

`src/master.py` is rewritten with both EU-5 (Deepgram live WebSocket ring-tail) and EU-6 (`CursorAgentSession`) integrated. On WAKE_DETECTED, `agent.prepare()` pre-spawns the agent subprocess and a `_CaptureSession` thread opens the Deepgram live WebSocket and tails the ring buffer at 20 ms intervals. On VAD_STOPPED, the thread is stopped, `send_finalize()` flushes the final transcript, and `agent.run(transcript)` streams the response to stdout.

The Pi validation session needs to confirm:
1. Deepgram live WebSocket connects and accumulates `is_final` transcripts correctly
2. KeepAlive fires during silence (no NET-0001 disconnect)
3. `agent.prepare()` + `agent.run()` produce a streaming response visible on stdout
4. Multi-turn: 3–5 consecutive turns without degradation
5. Ctrl+C from CAPTURE state — clean two-phase shutdown with no Pi reboot

See `spec/implementation_framework.md` EU-5 and `archive/2026-04-04_streaming_architecture_analysis.md` for full design rationale. See `spec/agent_session_spec.md` for the `AgentSession` interface contract.

### Step 7 closes on EU-5 Pi session

`forked_assistant/` is the delivery vehicle for `starting_brief.md` step 7 (agentic layer → text response). Step 8 (TTS → audio output) is driven from `starting_brief.md` scope and requires no changes to the recorder child or the process architecture.

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
