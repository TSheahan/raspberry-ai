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

**Requires Pi + ReSpeaker.** This track exercises real hardware.

**Estimated scope:** ~200 lines (processor adaptations + stub + harness). One or two Pi sessions.

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
- Ctrl+C clean from DORMANT, WAKE_LISTEN, CAPTURE — no Pi reboot
- No false wake detections after CAPTURE→WAKE_LISTEN (OWW reset proven)

**Estimated scope:** ~30 lines net new (mostly wiring); most code comes from the tracks. One Pi session.

---

### EU-4: Master Process — Batch Mode

**Goal:** Build the master process with batch-mode utterance processing (current STT + Claude pattern).

**Deliverable:** `forked_assistant/master.py` — the main entry point that:
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

**Estimated scope:** ~120 lines. One session, assuming EU-3 is proven.

---

### EU-5: Master Process — Streaming Extension

**Goal:** Add streaming STT support so the master can tail the ring buffer for extended dictation.

**Deliverable:** Streaming mode in master that:
1. On WAKE_DETECTED: opens a Deepgram live WebSocket session
2. Tails ring buffer, sending chunks to the WebSocket
3. Receives partial transcripts
4. Uses a configurable termination policy (VAD_STOPPED after N seconds silence, explicit command, timeout)

**This is a future effort unit.** Not required for first working delivery. Listed here for architectural awareness — the ring buffer + signal design was chosen specifically to enable this without changing the recorder child.

**Estimated scope:** ~100 lines added to master. Requires Deepgram live API integration (separate from the file-based API used in batch mode).

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
           EU-4 (Master process — batch mode)
             │
             ▼
           EU-5 (Master process — streaming)  ← future
```

EU-1, EU-2, EU-3a can be done in one session. EU-3b and EU-3c are parallel — assign to separate sessions. EU-3d is the merge, requires both tracks complete. EU-4 is mostly rearranging proven code. EU-5 is an extension.

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
    master.py                    ← EU-4
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
- When testing on Pi, always test Ctrl+C at every state. The shutdown path is where the single-process architecture failed; it must be proven clean in the two-process architecture at every stage.
- If a session hits a wall (SharedMemory doesn't work, core pinning fails, Pipecat doesn't survive fork), document the failure in a new markdown file and stop. Don't work around it without recording what happened.
