# Implementation Framework

**Date:** 2026-04-01
**Status:** Plan for implementation sessions
**Parent:** `forked_assistant/architecture.md`

---

## Guiding Principles for Implementation Sessions

### Read the specs first

Before writing code, read `architecture.md`, `interface_spec.md`, and `recorder_state_spec.md` in full. These contain hard-won constraints from the step 7 crash analysis (13 versions, multiple Pi reboots). The constraints are not suggestions — they represent failure modes that were hit and diagnosed on real hardware.

Key constraints that must not be relaxed:

- **Stream ops (stop/start) must be async tasks, never synchronous from Pipecat callbacks.** Violating this causes PortAudio deadlock or USB fault.
- **OWW full 5-buffer reset on every ungate transition.** Skipping this produces false-positive wake detections at score ~0.86.
- **Silero LSTM reset before first frame of new capture.** Stale hidden states contaminate the first utterance.
- **No ONNX workloads concurrent with each other.** OWW and Silero must run in non-overlapping phases. The Pi 4 does not have the CPU budget for both.

### Deposit all session state to specs/docs before ending

Per project convention: any session that produces design decisions, discovered constraints, or implementation choices must update the relevant spec or create a new document. Future sessions (potentially with different models) rely on written state, not conversation memory.

### Reference code

The working single-process implementations in `archive/step7/` are the source of truth for Pipecat integration patterns:

- `archive/step7/2026-04-01_voice_pipeline_step7_v10a.py` — representative version with stream suspension, gated VAD, crash-isolation gates
- `archive/step7/2026-04-01_voice_pipeline_step7_v11.py` — PipelineState breakout pattern (state object, weakrefs, centralized transitions)
- `archive/step7/2026-04-01_state_breakout_v10_to_v11.md` — detailed diff analysis of the state object refactor
- `archive/step7/2026-04-01_crash_analysis.md` — consolidated crash investigation (v01–v04)

Distilled knowledge from these files is also available in `memory/pipecat_learnings.md`, `memory/shutdown_and_buffer_patterns.md`, and `memory/architecture_decisions.md`.

---

## Effort Units

Implementation is ordered to prove the riskiest components first and build incrementally.

### EU-1: SharedMemory Smoke Test

**Goal:** Confirm that `multiprocessing.shared_memory.SharedMemory` works across a `multiprocessing.Process` fork on the Pi 4's Python version.

**Deliverable:** A standalone script (`test/smoke_test_shm.py`) that:
1. Master creates a named SharedMemory segment
2. Spawns a child process
3. Child opens the segment by name, writes a counter in a loop
4. Master reads the counter, prints values
5. Both processes clean up and exit

**What this proves:** SharedMemory works on ARM64 Linux with the project's Python. Atomic-width reads of uint64 write_pos are coherent across processes without locks.

**Risk addressed:** If SharedMemory doesn't work (unlikely but possible on some Python builds), we need to know before building on it.

**Estimated scope:** ~40 lines. One session, one file.

---

### EU-2: Ring Buffer Module

**Goal:** Implement the ring buffer read/write primitives as a standalone module.

**Deliverable:** `src/ring_buffer.py` containing:
- Constants: `HEADER_SIZE`, `RING_SIZE`, `SHM_SIZE`, `SHM_NAME`, audio format constants
- `RingBufferWriter` class: init from SharedMemory, `write(frame_bytes)`, `write_pos` property
- `RingBufferReader` class: init from SharedMemory, `read(start_pos, end_pos)` → bytes, staleness check
- Header pack/unpack utilities

**Test:** Extend the EU-1 smoke test to use the ring buffer module — child writes audio-sized frames, master reads spans.

**Estimated scope:** ~80 lines module + ~30 lines test extension.

---

### EU-3: Recorder Child — Parallel Track Breakdown

EU-3 is broken into four sub-units. EU-3b and EU-3c are **parallel** — they can be developed independently by separate sessions. EU-3d is the merge.

Stub boundary, method signatures, and both stub implementations are fully specified in **`stub_contracts.md`**. Read it before starting EU-3b or EU-3c.

---

#### EU-3a: RecorderState Skeleton

**Goal:** Implement `RecorderState` with real state machine logic and real signal emission method signatures, but with downstream port bodies left as `raise NotImplementedError`. This gives both parallel tracks a common base class to inherit from.

**Deliverable:** `src/recorder_state.py` containing:
- `RecorderState` class with all properties, `set_phase()` orchestration, and all method signatures from `stub_contracts.md`
- `__init__` accepts `pipe: Connection | None` and `shm: SharedMemory | None` (None during Track 2)
- Downstream port methods (`write_audio`, `signal_*`) raise `NotImplementedError` — subclasses provide bodies
- Side-effect hooks for `set_phase()` transitions defined but also raise `NotImplementedError` (`_start_stream`, `_stop_stream`, `_reset_oww_full`, `_reset_silero`)

**Why this first:** Both tracks import `recorder_state.py`. If EU-3a is complete, EU-3b and EU-3c can each subclass without diverging on method names or signatures. EU-3a can be done in the same session as EU-2 (it is ~60 lines).

---

#### EU-3b: Track 1 — IPC/Buffer Harness (parallel)

**Goal:** Prove the process boundary: SharedMemory, Pipe, fork, core pinning, and shutdown. No Pipecat. No PyAudio. No ONNX.

**Deliverable:** `test/track1_ipc_harness.py` — a self-contained two-process script:
- Child: `RecorderState` subclass with real downstream port (ring writes, pipe sends), driven by `FakeAudioDriver`
- Master: reads ring buffer spans, sends commands, prints events

See `stub_contracts.md` — Track 1 Spec for the full `FakeAudioDriver` implementation and master-side harness sequence.

**What this proves:**
- SharedMemory cross-process: named segment, write_pos coherence, ring wrap-around
- Pipe bidirectional: commands master→child, signals child→master
- Fork + core pinning on Pi ARM64
- State transitions via pipe commands (command listener calling `set_phase`)
- Write rate at real 20ms cadence stressing the ring over a full wake/capture cycle
- Shutdown clean from WAKE_LISTEN, CAPTURE, mid-cycle

**Can run on Pi without ReSpeaker or any audio hardware.** All audio is synthetic.

**Estimated scope:** ~120 lines. One Pi session.

---

#### EU-3c: Track 2 — Pipecat Pipeline Harness (parallel)

**Goal:** Prove the Pipecat pipeline adaptation: `GatedVADProcessor` and `OpenWakeWordProcessor` modified for `RecorderState`, `RingBufferWriter`, state transitions, and stream lifecycle. No fork. No SharedMemory. No Pipe.

**Deliverable:** `test/track2_pipeline_harness.py` — a single-process script:
- Uses `RecorderStateStub` (subclasses EU-3a's `RecorderState`, overrides downstream port to collect events)
- Full Pipecat pipeline: `transport.input() → GatedVADProcessor → OpenWakeWordProcessor → RingBufferWriter`
- `direct_command_driver` coroutine monitors `state.events` and calls `state.set_phase()` directly

See `stub_contracts.md` — Track 2 Spec for `RecorderStateStub` implementation and command driver pattern.

**What this proves:**
- Processor adaptations from v10a work with `RecorderState` interface
- OWW detects wake word and calls `signal_wake_detected()`
- Silero fires `signal_vad_started()` / `signal_vad_stopped()` via event handlers
- Phase gating: OWW active only in WAKE_LISTEN, Silero active only in CAPTURE
- OWW full reset fires on CAPTURE→WAKE_LISTEN transition
- Silero LSTM reset fires on →CAPTURE transition
- Stream ops (start/stop PyAudio) sequence correctly without deadlock
- Ctrl+C exits cleanly from any state
- Duty cycle within budget (async predict eliminates event loop blocking)

**Requires Pi + ReSpeaker.** This track exercises real hardware.

**EU-3c extended scope — async OWW predict: complete (2026-04-02)**

Duty cycle measurement revealed OWW `model.predict()` blocked the event loop for 23.7ms mean (119% of 20ms frame budget) on every 4th frame. Implemented and proven:

1. Reordered `OpenWakeWordProcessor.process_frame`: push_frame before predict ✅
2. Wrapped predict in `asyncio.to_thread()` (ONNX releases GIL) ✅
3. Added `_drain_oww_predict()` in `RecorderState.set_phase()`: awaits pending predict on wake_listen→capture (prevents concurrent ONNX) ✅
4. Re-ran duty cycle: wake_listen utilization 66% → 6%, 0 frames over budget (was 33%) ✅

**Instrumentation puzzle resolved:** The 2× multiplier (223 >20ms frames vs 112 predict calls) was direct event loop contention — a blocked predict delayed the next frame's entry stamp, causing it to also measure >20ms. Confirmed by async: with predict off the event loop, both frames land in the 0–5ms bucket. See `memory/architecture_decisions.md` — OWW Duty Cycle Characterization for full before/after data.

**`_predict_times` is a `deque(maxlen=500)`** — rolling window, safe for extended runtime. `_predict_count` tracks lifetime total for summary accuracy.

**Estimated scope:** ~200 lines (processor adaptations + stub + harness) + ~30 lines (async predict + drain guard). Completed in two Pi sessions.

---

#### EU-3d: Merge

**Goal:** Combine Track 1's real downstream port with Track 2's real Pipecat pipeline into a single `recorder_child.py` + `test_harness.py`.

**Deliverable:**
- `forked_assistant/recorder_child.py` — complete recorder subprocess: real `RecorderState` subclass with both ports real, full Pipecat pipeline, command listener
- `forked_assistant/test_harness.py` — master-side harness (Track 1's master pattern, exercising a live recorder child)

**Merge checklist** (from `stub_contracts.md` — EU-3d Merge Contract):
1. Verify `RecorderState.__init__(pipe=None, shm=None)` sets deferred — real values injected before READY
2. Verify all signal emission methods produce dicts matching `interface_spec.md` exactly
3. Verify `write_audio()` delegates to `ring_buffer.RingBufferWriter`
4. Verify `state.write_pos` is included in signal payloads

**Success criteria (full EU-3):**
- Complete 3 consecutive wake→capture→VAD cycles, ring spans readable by master
- Ctrl+C spot-check from at least one active state — no Pi reboot (full per-state coverage not required; EU-3c's extended real-hardware runs have proven the shutdown path)
- No false wake detections after CAPTURE→WAKE_LISTEN (OWW reset proven)

**Estimated scope:** ~30 lines net new (mostly wiring); most code comes from the tracks. One Pi session.

---

### EU-4: Master Process — Batch Mode

**Goal:** Build the master process with batch-mode utterance processing (current STT + Claude pattern).

**Deliverable:** `src/master.py` — the main entry point that:
1. Creates SharedMemory and Pipe
2. Spawns recorder child, pins to core 0
3. Sends SET_WAKE_LISTEN
4. On WAKE_DETECTED: sends SET_CAPTURE
5. On VAD_STOPPED: reads ring buffer span, transcribes via Deepgram, sends to Claude
6. On response complete: sends SET_WAKE_LISTEN
7. Handles Ctrl+C → SHUTDOWN sequence

**Implementation notes:**
- The cognitive loop (`_transcribe` + `run_claude`) can be lifted almost verbatim from v10a's `UtteranceCapturer._cognitive_loop` and `_transcribe`
- `asyncio.to_thread` for blocking STT and Claude calls, same as v10a
- Ring buffer read replaces the `self._chunks` accumulation pattern

**EU-4 complete (2026-04-03). All success criteria met.**

Master is synchronous (no asyncio in the master process). The event loop blocks on `pipe.recv()`, processes signals, and runs the cognitive loop inline. During the cognitive loop, the recorder child is already back in `wake_listen` (SET_WAKE_LISTEN is sent before the cognitive loop starts). Pipe messages from the child buffer in the kernel during processing; drained on return. A `processing` flag gates WAKE_DETECTED to prevent overlapping cognitive loops.

STT uses `DeepgramClient.listen.rest.v("1").transcribe_file()` (file-based batch API). Claude uses `claude -p` subprocess, same as v10a.

**First Pi run findings (2026-04-03):**

1. **Audio driver buffer overrun.** Occurred during the run. Root cause not yet diagnosed — likely PyAudio buffer pressure during the cognitive loop or ring buffer write timing. Investigate before the next session.

2. **Transcription did not succeed.** May be a consequence of the overrun (corrupted or empty audio bytes), or a Deepgram API call issue. Needs isolation — see debug strategy below.

**Debug strategy for next session:**

- Add a `--save-wav` flag (or env var `SAVE_CAPTURE_WAV=1`) to `master.py` that writes each captured ring buffer span to a timestamped `.wav` file in a scratch directory before sending to Deepgram. This lets you:
  - Inspect captured audio independently of STT (play it back, check duration and content)
  - Re-run transcription offline against the saved file to confirm the API call
  - Confirm the ring buffer read is producing valid audio (not zeros, not truncated)
- Implement as a thin wrapper in `cognitive_loop()` — one `wave.open()` write after `ring_reader.read()`, before `transcribe()` is called. Zero impact on normal operation when disabled.

**Second Pi run findings (2026-04-03, run 2):**

1. **Capture and STT working.** WAKE_DETECTED (score 0.912) → CAPTURE → VAD cycle completed successfully. Ring buffer read produced 120320 bytes (3.76s), rms=695.1, zeros=0. WAV saved, Deepgram transcribed "Hello?" with 1.71s latency. Adopting WAKE_DETECTED as the capture commencement signal (replacing VAD_STARTED) resolved the truncated transcript issue from run 1.

2. **Queue depth: clean throughout.** All duty cycle reports showed q_max=0. No QDEPTH ALARMs fired. Utilization peaked at 26% during capture. This confirms the prior analysis (`archive/alarming_queue_depths/`) — the crash vector is not pipeline backpressure.

3. **Pi crash during Ctrl+C shutdown.** `client_loop: send disconnect: Connection reset` — Pi rebooted. The child's `finally` block never ran (no `[QDEPTH]` summary, no `[child] exiting`).

**Root cause: SIGINT race condition.** ^C delivers SIGINT to the entire process group. The child's signal handler called `task.cancel()` directly, bypassing `set_phase("dormant")`. This meant `stop_stream()` was called from the CancelFrame handler while the PortAudio callback thread was still active — the exact race documented in `shutdown_and_buffer_patterns.md` (Root Cause 3). The spec (interface_spec.md §3 Shutdown sequence) requires the child to tear down exclusively via the SHUTDOWN pipe command, which ensures stream-stop-first ordering.

**Fix applied — two-phase shutdown protocol:** The child now handles SIGINT, SIGTERM, and SHUTDOWN pipe commands through a single `_initiate_shutdown()` with a once-only guard. The safe sequence is always: send `SHUTDOWN_COMMENCED` → `set_phase("dormant")` (stops stream, 100ms settle) → `task.cancel()` (pipeline drains) → cleanup → send `SHUTDOWN_FINISHED`. The master waits for `SHUTDOWN_FINISHED` on all exit paths (KeyboardInterrupt, normal return, EOFError) before cleaning up SharedMemory. `cancel_with_stream_stop` checks `stream.is_active()` to avoid redundant stop on the SHUTDOWN path where `set_phase("dormant")` already stopped the stream. See updated `interface_spec.md` §3 Shutdown sequence for the full protocol.

**Third Pi run (2026-04-03, run 3) — EU-4 success criteria met:**

- Wake detected (score 0.879), 3.12s capture, rms=610.3, Deepgram transcribed "Hello." in 1.82s
- Queue depth: q_max=0 throughout, 0 alarms, 0/485 frames over 20ms budget
- Ctrl+C from WAKE_LISTEN: clean two-phase shutdown — SHUTDOWN_COMMENCED received, stream stopped, pipeline drained, SHUTDOWN_FINISHED received, `[master] done`, no Pi reboot

Remaining validation before step 7 closes: (1) confirm Claude response text prints on a full turn; (2) multi-turn stability (3–5 consecutive turns); (3) Ctrl+C from CAPTURE state. These are a single Pi session.

**TODO — logging uplift:** All diagnostic output in `recorder_child.py`, `master.py`, and `recorder_state.py` currently uses bare `print()`. This is sufficient for early debugging but should be replaced with structured `logging` calls (using a per-module logger, configurable level, and consistent format) before the system is considered production-ready. No functional change — purely a logging hygiene pass. Track as a post-EU-4 cleanup task alongside multi-turn validation.

**Estimated scope:** ~120 lines. One session, assuming EU-3 is proven.

---

### EU-5: Master Process — Streaming STT

**Goal:** Replace the batch STT path with a live Deepgram WebSocket session that tails the ring buffer as the recorder child writes it. Streaming STT is required before step 7 closes — it is not optional. The ring buffer + signal protocol was designed specifically to make this possible without touching the recorder child.

**Deliverable:** Streaming mode in `src/master.py` that:
1. On WAKE_DETECTED: opens a Deepgram live WebSocket session **and** calls `agent.prepare()` concurrently (agent pre-spawn hides startup latency behind the STT window)
2. Spawns a ring buffer tail thread that reads newly written frames and sends them to the WebSocket
3. Receives and accumulates `is_final` transcripts
4. Terminates on VAD_STOPPED (the primary policy); sends `send_finalize()` → `send_close_stream()`, assembles final transcript
5. Passes completed transcript to `agent.run(transcript)` (EU-6's `AgentSession`); iterates text deltas to TTS
6. Sends SET_WAKE_LISTEN and returns to listening

**Termination policy note:** VAD_STOPPED is the initial termination trigger (same timing as EU-4's batch mode). The architecture supports extending this to a timeout fallback or explicit command without changing the recorder child.

**Recorder child changes:** None. The child already writes continuously to the ring buffer and sends VAD_STARTED / VAD_STOPPED over the pipe. EU-5 is entirely a master-side change.

**Implementation notes:**

- **API correction (SDK v6):** The original spec referenced `dg_client.listen.live.v("1")` — this is the v2/v3 SDK pattern and is no longer correct. Current SDK (v6, March 2026) uses:
  ```python
  with dg_client.listen.v1.connect(
      model="nova-3", encoding="linear16", sample_rate=16000,
      channels=1, language="en-US", smart_format=True,
      interim_results=True, endpointing=300,
  ) as connection:
      connection.on(EventType.MESSAGE, on_message)
      listen_thread = threading.Thread(target=connection.start_listening, daemon=True)
      listen_thread.start()
      # ... ring tail sends connection.send_media(audio_bytes) ...
      connection.send_finalize()
      time.sleep(0.2)
      connection.send_close_stream()
  ```
  See `archive/2026-04-04_streaming_architecture_analysis.md` §2.1 for full corrected pattern.

- **KeepAlive is mandatory:** Deepgram closes the WebSocket with `NET-0001` after 10s of silence. The ring tail thread must send `connection.send_keep_alive()` every 3–4s when `write_pos` has not advanced. Critical for dictation mode with long inter-sentence pauses.

- **Mixed mode (recommended):** Keep Silero VAD as the dispatch trigger. Deepgram's own endpointing is advisory only. `is_final=True` results accumulate throughout the capture session; `send_finalize()` flushes the last fragment on VAD_STOPPED. Net STT latency: ~50–150ms (vs ~1.8s batch). See `archive/2026-04-04_streaming_architecture_analysis.md` §3.2 for full analysis.

- **Ring tail thread:** `threading.Thread` alongside the blocking `pipe.recv()` main loop. Polls `ring_reader.write_pos` at ~20ms intervals.

- **`cognitive_loop()` signature change:** `(audio_bytes, dg_client)` → `(transcript: str, agent: AgentSession)`. The `transcribe()` and `run_claude()` / `stub_claude()` functions are retired.

**Estimated scope:** ~80 lines added to master. One Pi session (EU-5 + EU-6 integration land together).

**Code written (2026-04-04):** `src/master.py` rewritten with EU-5 streaming and EU-6 agent wiring integrated. Key implementation notes:
- `EventType` imported from `deepgram.core.events` (SDK v6 path, confirmed from official docs).
- `on_message` callback checks `message.type == "Results"` and `message.is_final` before accumulating.
- `_CaptureSession` class encapsulates transcript accumulator + stop_event + thread handle.
- Ring tail runs in a `threading.Thread` (daemon). On `VAD_STOPPED`, `stop_event.set()` → `thread.join(timeout=5)` → `send_finalize()` → `send_close_stream()` inside the context manager.
- `agent.prepare()` called synchronously on `WAKE_DETECTED` (Popen is non-blocking; returns immediately).
- `cognitive_loop(transcript, agent)` replaces the old `cognitive_loop(audio_bytes, dg_client)`. Prints text chunks from `agent.run()` incrementally.
- Batch STT (`transcribe()`, `run_claude()`, `stub_claude()`, `_save_wav_debug()`) removed.
- Pi validation required before marking EU-5 and EU-6 complete.

---

### EU-6: Agent Module — `AgentSession` Abstraction

**Goal:** Replace the blocking `run_claude()` / `stub_claude()` call with a modular agent abstraction that supports pre-spawning on WAKE_DETECTED, streaming text delta output for TTS, and session continuity across conversation turns.

**Deliverable:** `src/agent_session.py` containing:
- `AgentSession` — abstract base class defining the interface `master.py` uses
- `CursorAgentSession(AgentSession)` — Cursor CLI implementation (proven on Pi via smoke test)
- TypedDict event schema, `parse_stream_line()`, `extract_delta_text()` utilities
- Word-boundary buffer: holds tail of each delta up to last whitespace, yields complete portions only

**Why this replaces the original EU-6 scope:**
- Cursor CLI (`~/.local/bin/agent`) is preferred over Claude CLI: not subject to Claude token quota exhaustion; runs under Cursor subscription; proven working on Pi ARM64 (2026-04-04 smoke test).
- Pre-spawn pattern: agent process is spawned on WAKE_DETECTED and waits with stdin open. By VAD_STOPPED the process is already initialised — startup latency is hidden behind the Deepgram streaming window.
- Session continuity: `--resume session_id` passes context across turns within a configurable time window (`AGENT_RESUME_WINDOW_SECS`, default 300s). After the window expires, a fresh session is started automatically.
- Interchangeability: `AgentSession` base class allows future backends (alternative models, direct API) to be swapped in without touching `master.py`.

**Interface contract:**
```python
class AgentSession:
    def prepare(self) -> None: ...          # pre-spawn on WAKE_DETECTED
    def run(self, transcript: str) -> Iterator[str]: ...  # yield TTS-safe text deltas
    def close(self) -> None: ...            # cleanup
    @property
    def session_id(self) -> str | None: ...
    @property
    def last_turn_time(self) -> float: ...  # monotonic; 0.0 before first turn
```

**Cursor CLI invocation:**
```
agent -p --output-format stream-json --stream-partial-output
      --force --yolo --trust
      --workspace <workspace>
      --model <model>
      [--resume <session_id>]
```
stdin receives the transcript; stdout is a stream of newline-delimited JSON events. See `spec/agent_session_spec.md` for full contract and `archive/2026-04-04_wrapped_cursor_agent_context.md` for stream-json schema reference.

**Known regression:** Claude CLI `-p` with `subprocess.run` has a known empty-stdout regression (v2.1.83). Cursor CLI via `stream-json` / `Popen` is not affected.

**Status:** `src/agent_session.py` is written. Pi integration is complete in `src/master.py` (2026-04-04) — `prepare()` is called on WAKE_DETECTED and `run()` drives the cognitive loop. Pi validation is the remaining step.

**Estimated scope:** ~160 lines (`agent_session.py` written). Pi integration ~30 lines in `master.py`.

---

### Step 7 Completion Criteria and Step 8 Handoff

`forked_assistant/` closes step 7 when all of the following are confirmed on Pi:

1. ✓ Wake → capture → VAD cycle stable (EU-3d, EU-4)
2. ✓ Ring buffer span read correct, STT produces transcript (EU-4 runs 2–3)
3. ✓ Shutdown clean from any state, no Pi reboot (EU-4 run 3)
4. ☐ Claude response text printed on a full turn (EU-4 validation run)
5. ☐ Multi-turn: 3–5 consecutive turns without degradation (EU-4 validation run)
6. ☐ Streaming STT via Deepgram live WebSocket (EU-5)
7. ☐ Streaming Claude response with incremental text output (EU-6)

**Step 8 (TTS → audio output) is driven from `starting_brief.md` scope**, not from `forked_assistant/`. The handoff point is: step 7 delivers text response to stdout; step 8 feeds that text to Piper and plays audio through device index 0. The `forked_assistant/` architecture requires no further changes for step 8 — TTS runs in the master process (cores 1–3) after EU-6's `run_claude_streaming()` returns each text chunk.

### Post-EU-6: Step 7 Delivery Packaging

After EU-6 is confirmed on Pi and all step 7 completion criteria are checked, perform the following before starting step 8:

**Deliverable refactor:** The four platform files are the step 7 delivery artifact:
- `src/ring_buffer.py`
- `src/recorder_state.py`
- `src/recorder_child.py`
- `src/master.py`

Copy or move these from `forked_assistant/src/` to `mvp-modules/deliverables/step7/`. This mirrors the pattern established in `mvp-modules/deliverables/` for prior steps and marks `forked_assistant/` as a concluded development effort.

**Markdown updates at delivery boundary:**
- `mvp-modules/starting_brief.md` — record step 7 complete, note the two-process architecture as the delivery mechanism, summarise latency observations from EU-5/EU-6 runs
- `mvp-modules/forked_assistant/AGENTS.md` — mark all EUs complete, update What's Next to step 8
- `spec/implementation_framework.md` — mark EU-5, EU-6 complete; record final run data
- `memory/architecture_decisions.md` and `memory/shutdown_and_buffer_patterns.md` — any remaining session findings
- `mvp-modules/INDEX.md` — add step 7 deliverables entry if the index tracks deliverables

**Scope note:** `forked_assistant/spec/`, `test/`, and `archive/` remain in place as the development record. Only `src/` is promoted to `deliverables/`. The specs are the supporting documentation for the delivered code and should be cross-referenced from the deliverables entry.

---

## Prerequisite Smoke Tests (not in EU integration path)

These tests are not effort units in the recorder-child build sequence, but they are **prerequisites before EU-3c results can be trusted** for accuracy-sensitive work (duty cycle measurement, VAD sensitivity tuning, beam-forming). Each is a standalone script in `test/`.

Status column: **not started** / in progress / complete.

| ID | Deliverable | Goal | Status |
|----|-------------|------|--------|
| P-1 | `test/smoke_respeaker_channels.py` | Determine actual channel count delivered by PyAudio for device index 1. The ring buffer and all inference code assume 16 kHz int16 mono. If the ReSpeaker presents as multi-channel, all downstream processing is silently wrong. | complete — executed 2026-04-02 |
| P-2 | `test/smoke_beamform_shim.py` | If P-1 shows multi-channel data: prove that a channel-extraction shim (or USB tuning module beam-forming) produces clean mono that OWW and Silero respond to correctly. Answers: "do Track 2 results hold with correct channel handling?" | **not pursued** — 1-ch mono is sufficient for OWW, VAD, and STT; multi-channel not warranted |

**P-1 / P-2 fully closed (2026-04-02).** See `memory/architecture_decisions.md` — "ReSpeaker Audio Configuration" for complete findings. Summary:

- 1-ch at 16kHz is confirmed correct for OWW, VAD, and STT. Deepgram STT confirmed good quality.
- 2-ch/4-ch silence was a format mismatch (paInt16 vs AC108 native S32_LE); 4-ch is technically accessible but not pursued.
- Channel provenance: ADC1 (channel 0) only — ALSA plug default, no ttable, one physical mic dominant (~90% certainty from tap probe).
- PGA gain is not in the 1-ch signal path. PGA changes at 0/10/20/28 dB produced no change in noise floor (~56 RMS constant). ADC digital volume at 47.25 dB is the fixed active gain stage. No software quality lever available.
- No further audio quality investigation warranted. EU-3 continuation is unblocked.

**What P-1 should do:**
- Open PyAudio, print full device info for index 1 (max input channels, default sample rate)
- Attempt to open a raw input stream at 1-ch, 2-ch, and 4-ch; print success/failure and captured frame sizes
- Print the first 8 int16 samples from each successful configuration so interleaving is visible
- No inference — this is a hardware probe only

**What P-2 should do (depends on P-1):**
- Capture N seconds of multi-channel audio from the ReSpeaker
- Apply channel extraction (take channel 0) and optionally USB tuning beam-forming if the `tuning` module is available
- Feed the resulting mono stream to OWW in real-time; report detection events and scores
- Compare to a baseline mono-only capture at the same time to confirm equivalence
- If results differ significantly, document the delta as a constraint for EU-3c

---

## Dependency Graph

```
EU-1 (SharedMemory smoke test)
  │
  ▼
EU-2 (Ring buffer module)
  │
  ▼
EU-3a (RecorderState skeleton)
  │
  ├──────────────────────┐
  ▼                      ▼
EU-3b (Track 1:        EU-3c (Track 2:        ← parallel
  IPC/buffer harness)    Pipecat pipeline)
  │                      │
  └──────────┬───────────┘
             ▼
           EU-3d (Merge → recorder_child.py + test_harness.py)
             │
             ▼
           EU-4 (Master process — batch mode)  ← complete
             │
             ├── EU-4 validation (Claude response confirmed, multi-turn, Ctrl+C from CAPTURE)
             │
             ▼
           EU-5 (Streaming STT — Deepgram live WebSocket + ring buffer tail)
             │                         ┐
             │                         EU-6 (Agent module — AgentSession + CursorAgentSession)
             │                         │  src/agent_session.py written; Pi integration in EU-5 session
             ▼                         ┘
        master.py restructure (EU-5 + EU-6 Pi session — cognitive loop wiring)
             │
             ▼
        *** Step 7 complete — forked_assistant/ closes ***
             │
             ├── Delivery packaging: src/ → mvp-modules/deliverables/step7/
             ├── starting_brief.md step 7 marked complete
             │
             ▼
        Step 8: TTS → audio output  (driven from starting_brief.md)
```

EU-1 through EU-4 are complete. EU-5 and EU-6 code is written — `src/master.py` contains the streaming STT ring-tail and `CursorAgentSession` wiring (2026-04-04). The recorder child is frozen. Pi validation is the remaining step to close step 7. Step 8 is out of scope for `forked_assistant/`.

---

## File Layout

```
forked_assistant/
  spec/
    architecture.md              ← design rationale and consolidated learnings
    interface_spec.md            ← ring buffer layout, signal protocol, lifecycle
    recorder_state_spec.md       ← recorder child state machine, RecorderState object
    stub_contracts.md            ← EU-3 parallel track seam: method signatures, stub specs
    implementation_framework.md  ← this file (effort units, ordering, guidance)
  src/
    ring_buffer.py               ← EU-2
    recorder_state.py            ← EU-3a  (RecorderState base class)
    recorder_child.py            ← EU-3d  (merged, permanent)
    agent_session.py             ← EU-6   (AgentSession base + CursorAgentSession; written)
    master.py                    ← EU-4/EU-5/EU-6 (streaming STT + agent integration in Pi session)
  test/
    smoke_test_shm.py            ← EU-1
    track1_ipc_harness.py        ← EU-3b  (throwaway after merge)
    track2_pipeline_harness.py   ← EU-3c  (throwaway after merge)
    test_harness.py              ← EU-3d  (permanent)
  archive/                       ← superseded snapshots
```

---

## Notes for Sessions Using Sonnet or Other Models

- The spec documents are self-contained. Read them; they have the constraints and the reasoning.
- `v10a.py` is the reference implementation for processor logic. `v11.py` is the reference for the state object pattern. Don't read all 13 versions — these two plus the specs are sufficient.
- The `GATE_*` diagnostic flags in v10a are a debugging pattern worth preserving in the recorder child during development. They allow isolating crash sources by selectively disabling processing stages.
- Spot-check Ctrl+C during Pi sessions. The shutdown path was the single-process failure mode; EU-3c's repeated real-hardware teardown (PyAudio + ONNX + Pipecat) has substantially retired that risk in the two-process architecture. Exhaustive per-state testing is no longer required at every stage — check it when testing new shutdown paths or after significant pipeline changes.
- If a session hits a wall (SharedMemory doesn't work, core pinning fails, Pipecat doesn't survive fork), document the failure in a new markdown file and stop. Don't work around it without recording what happened.
