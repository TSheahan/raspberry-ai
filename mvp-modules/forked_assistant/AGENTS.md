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
│   ├── tts.py                    ← EU-7: PiperTTS wrapper (Piper ONNX + PyAudio device 0)
│   └── master.py                 ← EU-5/EU-6/EU-7: streaming STT + agent + TTS
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
| EU-5 | Streaming STT — Deepgram live WebSocket + ring buffer tail | Complete (`src/master.py`, proven 2026-04-04) |
| EU-6 | Agent module — `AgentSession` abstraction + `CursorAgentSession` (Cursor CLI) | Complete (`src/agent_session.py`, integrated in `src/master.py`, proven 2026-04-04) |
| EU-7 | TTS — `PiperTTS` wrapper + `master.py` integration | Complete (`src/tts.py`, `src/master.py`, proven 2026-04-04) |

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

### Step 8 complete — quality improvements in progress

EU-7 proven 2026-04-04. Full pipeline run: "Tell me about the rules you know." → STT (0.45s) → CursorAgentSession (11.3s to result, 7.3s agent duration) → PiperTTS → audio through 3.5mm jack. Clean Ctrl+C from wake_listen. 2/8509 duty frames over budget (0.0%).

One quality item remains:

1. **Markdown stripping** (done — `tts.py`) — agent responses containing `**bold**` were read as "asterisk asterisk bold asterisk asterisk" by Piper. `_strip_markdown()` in `tts.py` now removes bold/italic, headers, list bullets, and inline code before synthesis.

**Live-sentence streaming done** (`agent_session.py`) — `run()` now yields sentence chunks live as streaming deltas arrive (`_flush_sentences()` buffers to `[.!?]` boundaries). Tail reconciliation against `result.result` handles three cases: normal prefix match (yield tail), nothing yielded live (yield full canonical), tool-call re-emission mismatch (flush buffer only). First audio expected ~2s after VAD_STOPPED on the next Pi run.

**Response brevity** is the remaining latency lever: a 173-token 4-point list produces ~54s of audio. The agent has a voice-interface rule but didn't apply it strongly here. This is a persona/prompt tuning concern, not a pipeline code issue.

### Step 9 — loop (wake → turn → wake)

Pipeline already loops: `SET_WAKE_LISTEN` is sent after `cognitive_loop` returns, and clean turns have been observed across multiple EU-5/EU-6 runs. Formal step 9 validation (3–5 complete turns, latency measurements table) follows the live-sentence streaming improvement.

## Completed Effort Units

### EU-7 — complete (2026-04-04)

PiperTTS proven on Pi. First full end-to-end voice turn: "Tell me about the rules you know."

- STT latency: 451ms ✓ (target < 1s)
- Agent duration: 7.3s (6.3s to first token; Cursor agent cold-start dominates)
- Time to first TTS audio: 11.3s post-VAD_STOPPED (end-of-stream batch yield — addressed by EU-7b live-sentence streaming)
- Total audio played: ~54s for 173-token 4-point list response
- Duty cycle: 2/8509 frames over 20ms budget (0.0%) during TTS playback window
- Clean Ctrl+C from wake_listen; SHUTDOWN_FINISHED → `[master] done` with no reboot

Post-run fix: `_strip_markdown()` added to `tts.py` — agent responses with `**bold**` rendered as "asterisk asterisk" by Piper before the fix.

### EU-5 + EU-6 — complete (2026-04-04)

Streaming STT + agent session proven on Pi across three runs (`scratch/executions/2026-04-04_EU-5_tests/`):

- **Run 1:** TRANSCRIPT "Hello." — Deepgram WebSocket connected and delivered `is_final`; `agent.prepare()` pre-spawned correctly; Ctrl+C while agent generating → clean shutdown.
- **Run 2:** TRANSCRIPT "Summarize what files you can see." — full streamed response: 21.4 s, 678 output tokens, `cache_read=35195`; master looped back to `wake_listen`; clean shutdown from `wake_listen`.
- **Run 3:** Full turn complete; master looped back to listening; clean shutdown from idle; 1/2817 frames over 20 ms budget (0.0%).

All EU-5 validation criteria met: Deepgram live WebSocket connects and accumulates `is_final` transcripts; KeepAlive fires (no NET-0001 disconnect); `agent.prepare()` + `agent.run()` produce streaming text response; 3 consecutive turns across runs without degradation; Ctrl+C from idle, wake_listen, and during agent generation all produce clean SHUTDOWN_FINISHED → `[master] done` with no Pi reboot.

**Step 7 closed.** The streamed text output from `agent.run()` is ready for TTS.

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
