# Architecture Decisions

Key design choices for the two-process forked assistant, with rationale rooted in step 7 findings.

## Why Two Processes

The single-process Pipecat pipeline (steps 4–7) proved that every pipeline component works individually. The blocking issue is their interaction during and after the cognitive loop: CPU contention between ONNX inference and the Claude subprocess, combined with unbounded Pipecat queues and a shutdown race in PortAudio's C layer.

Process isolation resolves all three simultaneously:

| Single-process problem | Two-process resolution |
|---|---|
| CPU contention: Claude competes with ONNX inference | Processes on separate cores, no competition |
| Queue growth: frames accumulate during cognitive loop | Ring buffer at constant rate, no unbounded queue |
| I2S/DMA starvation from CPU load | Recorder's dedicated core with SCHED_FIFO gives uninterrupted I2S attention |
| Shutdown race: PyAudio callback vs asyncio teardown | Recorder tears down independently; killed child doesn't reboot Pi |

## Why SharedMemory + Pipe (not sockets, not files)

**SharedMemory (`multiprocessing.shared_memory`):**
- Standard library, Python 3.8+. Uses `/dev/shm` (tmpfs) on Linux.
- Single-writer/single-reader ring buffer needs no locks.
- Write cursor as monotonic uint64, coherent via aligned access on ARM64.
- 512KB ring ≈ 16s lookback at 16kHz int16 mono.
- Audio frames (640 bytes per 20ms) written by memcpy, sub-microsecond.

**Pipe (`multiprocessing.Pipe()`):**
- Unix domain socket pair with pickle serialization.
- Message-oriented (each `send()`/`recv()` is one complete dict).
- Sub-millisecond latency, kernel-buffered (~64KB).
- Selectable fd for event loop integration.

**Design principle:** Separate data plane from control plane. Audio data (high volume, continuous) flows through shared memory. Control signals (low volume, sporadic) flow through the pipe. Mixing them in a single channel (as Pipecat's frame stream does internally) creates the access-pattern conflict that caused the original queue accumulation.

## Why VAD as Sensor, Not Gate

The recorder child reports observations: wake word detected, speech started, speech stopped. The master decides what those observations mean for the current interaction mode.

This decouples audio sensing from consumption policy:
- **Quick command mode:** VAD_STOPPED → "utterance complete, batch-transcribe"
- **Dictation mode:** ignore VAD_STOPPED unless silence exceeds N seconds, keep streaming
- **Future multi-turn:** use VAD_STOPPED to segment turns but don't stop the stream

The alternative (the recorder child interpreting VAD events and deciding when to stop) locks the interaction model into the recorder's logic.

## Why PipelineState Object (from v11)

Both processes use a centralized state object that owns all shared mutable state, exposes read-only properties, and executes side-effects on phase transitions. Processors hold only a reference to the state object, never to each other.

**Before (v10):** Cross-reference graph between processors. State mutations scattered across three classes. Weakref wiring done manually in `main()`. Adding a new state-dependent behavior required touching multiple classes.

**After (v11):** Hub-and-spoke. All transitions go through `state.set_phase()`. Side-effects (stream pause, ONNX reset, counter reset) are centralized. Adding new behavior means adding one method to the state object.

This pattern maps directly to the two-process design: the recorder child has its own `RecorderState` (DORMANT → WAKE_LISTEN → CAPTURE), and the master will have a `MasterState` with its own phase model.

## Why Core Pinning and SCHED_FIFO

Pi 4 has 4 ARM Cortex-A72 cores. The recorder child is pinned to core 0 via `os.sched_setaffinity()` immediately after fork. The master uses cores 1–3.

Pinning prevents the OS scheduler from migrating the recorder process during a latency-sensitive audio callback. The ReSpeaker 4-Mic HAT uses I2S (GPIO header) — audio is transferred via DMA into BCM2835's I2S controller. The PortAudio callback thread must service ALSA's period-ready notifications promptly; if the process is migrated or preempted at the wrong moment, ALSA period callbacks are delayed and the I2S DMA ring can overflow.

The child additionally runs at `SCHED_FIFO` priority 50 (real-time scheduling). Without this, any normal-priority process on core 0 (kernel workers, softirqs, other daemons) can preempt the recorder during ONNX inference and cause duty cycle spikes. Confirmed (2026-04-03): with SCHED_FIFO, duty cycle max during idle is 3.0ms; without it (earlier runs), max reached 49.6ms during the cognitive loop window.

## ReSpeaker Audio Configuration — Resolved (P-1 2026-04-02)

**Status: closed. 1-ch at 16kHz is the correct and sufficient configuration for all pipeline stages.**

**Driver note:** Raspberry Pi OS Trixie uses the [HinTak community fork](https://github.com/HinTak/seeed-voicecard) of seeed-voicecard for current-kernel compatibility. Same `asound_4mic.conf` / `ac108` plug structure as the original.

**P-1 findings (`test/smoke_respeaker_channels.py` run on Pi 2026-04-02):**

- Device: `seeed-4mic-voicecard: bcm2835-i2s-ac10x-codec0 (hw:3,0)`, 4 max input channels, 44100 Hz native default
- 1-ch @ 16kHz: opens OK, delivers real audio (plausible noise floor samples) ✓
- 2-ch @ 16kHz: opens without error, correct byte count, but delivers silence even with sound present
- 4-ch @ 16kHz: same — opens without error, silence only

The silence on 2-ch/4-ch is a **format mismatch**, not a hardware limit. The AC108 codec outputs S32_LE natively. P-1 probed with `paInt16`. The `hw:` ALSA device cannot serve S16_LE and returns zero-filled buffers silently. The 1-ch path works because `/etc/asound.conf` routes it through the `ac108` plug PCM, which performs S32_LE→S16_LE conversion. The large values in the initial 2-ch read were a stream initialization artifact (stale DMA buffer).

4-ch capture **is technically available** via `paInt32` (or `alsaaudio` with `PCM_FORMAT_S32_LE`) targeting the `ac108` named ALSA device — the seeed wiki documents `arecord -Dac108 -f S32_LE -r 16000 -c 4` as the canonical command. This path is not currently pursued (see design decision below).

**Design decision: 1-ch mono for all three pipeline stages**

OWW, VAD, and STT all use the same 1-ch 16kHz stream. This is a deliberate choice, not a fallback:

- **OWW**: designed for mono 16kHz — no adjustment needed or wanted
- **VAD**: an overlay on the capture stream; already functional, tunable via Silero params; the sensitivity question is not a channel issue
- **STT**: 1-ch mono is sufficient for transcript quality; adding channels multiplies ring buffer data volume and risks ALSA buffer overruns for marginal acoustic gain

The ring buffer architecture preserves the option to switch to multi-channel capture in the future (e.g., for beam-forming quality uplift) without changing the master or the signal protocol. That option is not exercised now.

**P-2 (beamform shim) is not pursued.** The prerequisite for P-2 was genuine multi-channel capture being necessary — it is not. VAD sensitivity issues are not channel-packing symptoms; investigate Silero params/thresholds directly.

**Channel provenance — ADC1 (channel 0) only (~90% certainty, 2026-04-02)**

The `ac108` plug has no `ttable` (routing table). ALSA's default plug behaviour for N→1 channel reduction is to take channel 0 and discard the rest. Tap probe testing (touching each of the 4 mic positions in turn while monitoring 1-ch RMS) showed one position clearly dominant, the other three producing significantly lower response. Confirmed with ~90% certainty: the 1-ch stream captures from ADC1 only, one physical microphone, not a mix.

Implication: microphone physical orientation on the hat matters. If the hat is mounted with ADC1 pointing away from the speaker, sensitivity will be reduced. This is a physical placement concern, not a software one.

**PGA gain investigation — closed, no software lever available (2026-04-02)**

AC108 state from `/etc/voicecard/ac108_asound.state`:
- ADC1–4 PGA gain: **0 dB** (value 0, dbvalue 0) on all four channels
- ADC1–4 digital volume: **47.25 dB** (value 222, dbvalue 4725) — the active gain stage

PGA changes tested via `amixer -c 3 sset 'ADC1 PGA gain'` at 0, 10, 20, and 28 dB. In all four runs, the ambient noise floor remained constant at ~56 RMS (seconds of silence measured consistently). If the PGA were in the 1-ch signal path, the noise floor would scale proportionally — at 28 dB it should reach ~1400. It did not.

**Conclusion: the PGA is not in the 1-ch ALSA plug path as accessed.** The `ac108` plug sources audio from a point in the codec chain that is downstream of the PGA, or the amixer writes don't propagate to the hardware register. Either way, PGA adjustment has no effect on the 1-ch output. The 47.25 dB ADC digital volume is the effective and fixed gain stage.

No viable software audio quality lever exists within the current driver and ALSA configuration. Further uplift would require direct AC108 register manipulation or opening at 44100 Hz 4-ch (S32_LE) and doing channel selection and SRC in application code — not warranted given STT results are already good.

**STT quality confirmed sufficient (2026-04-02)**

Deepgram batch-mode STT on captures from this pipeline returns good transcripts. Audio quality is not the current bottleneck. The mono path at 16kHz, as configured by the seeed-voicecard installer, is adequate for the intended use case.

## OWW Duty Cycle Characterization (2026-04-02)

**Status: implemented and proven (2026-04-02). EU-3c complete.**

Duty cycle instrumentation (bookend processors at pipeline head/tail) measured end-to-end per-frame pipeline traversal time during a live wake→capture→VAD cycle on Pi 4.

**Confirmed audio format:** 16kHz, 1-ch, 320 samples (640 bytes) per frame, 20ms cadence. Matches all assumptions.

**OWW predict is the dominant cost.** Measured on Pi 4 Cortex-A72, core 0:

| Metric | Value |
|--------|-------|
| Predict call frequency | 1 per 4 audio frames (1280-sample chunk) |
| Mean predict duration | 23.7ms (119% of 20ms frame budget) |
| p95 predict duration | 28.1ms |
| Max predict duration | 38.2ms |
| Wake_listen budget utilization | 66% (average across all frames) |

The processing pattern is **bimodal**: 50% of wake_listen frames measure 0–5ms (accumulate-only), 50% measure >20ms (predict-triggering or affected). The pipeline survives because 3 of 4 frames are fast (~1ms), keeping the running average under budget. But each predict call blocks the event loop for 24–38ms — no state transitions, ring writes, or command processing during that window.

Capture phase is clean: 7% utilization, all frames <5ms (Silero inference is much lighter than OWW).

**Instrumentation puzzle (tracked, not blocking):** The duty cycle histogram shows exactly 2× the predict call count as >20ms frames (223 >20ms vs 112 predict calls). The mechanism by which each predict causes 2 frames to measure >20ms in the bookend is likely an asyncio task scheduling artifact related to queue dwell time. This is tracked but deferred — further analysis is not warranted until the pipeline has broken-out OWW predict timing and the async optimization is in place. The operational characterization is unaffected: predict is the sole heavy operation.

**Decision: move OWW predict to async (`asyncio.to_thread`) — implemented and proven (2026-04-02)**


OWW's `model.predict()` produces a side-channel signal (`signal_wake_detected`), not a frame transformation. There is no data dependency between predict's result and the audio frame flowing downstream. Frames can be pushed immediately; predict runs in background.

ONNX runtime is a C++ extension that releases the GIL during inference. `to_thread` moves predict to a thread pool, freeing the event loop to process frames at steady 20ms cadence while ONNX compute runs concurrently.

Cost: one frame of wake detection latency (~20ms). Imperceptible given OWW already accumulates 80ms of audio per chunk, and the master's command polling adds ~100ms on top.

**Constraint: drain guard on phase transition.** When transitioning wake_listen→capture, any pending async predict must complete before Silero starts. This prevents concurrent ONNX sessions (the proven failure mode from step 7). Implemented as `_drain_oww_predict()` in `RecorderState.set_phase()` — called on wake_listen exit, before `_reset_silero()`.

**After (measured 2026-04-02, same Pi 4 Cortex-A72 core 0):**

| Metric | Before | After |
|--------|--------|-------|
| Wake_listen budget utilization | 66% | 6% |
| Frames over 20ms budget | 223/675 (33%) | 0/1194 (0%) |
| Wake_listen frame distribution | 50% <5ms, 50% >20ms (bimodal) | 100% <5ms (uniform) |
| Predict mean (thread pool) | 23.7ms | 27.9ms (+4ms dispatch overhead) |
| Capture budget utilization | 7% | 13% (Silero more visible at larger sample) |

The dispatch overhead (~4ms) is irrelevant — predict is now off the critical path entirely.

**Instrumentation puzzle resolved (2026-04-02).** The "2× predict count as >20ms frames" observation (223 >20ms frames vs 112 predict calls in the before data) is now explained: a synchronous predict blocking the event loop for ~24ms caused *both* the predict frame and the subsequent frame to measure >20ms in the bookend timing (the next frame's entry stamp was delayed by the blocked loop). With predict async, the subsequent frame sees the event loop immediately available — both frames measure <5ms. The 2× multiplier was direct event loop contention, not a queue dwell artifact.

## Capture Span Start: Wake Position, Not VAD Start (2026-04-03)

**Finding:** Using `VAD_STARTED write_pos` as the ring buffer span start discards the utterance onset — typically 1–2 seconds of audio including the opening word(s) of the query.

**Root cause:** Silero VAD's `start_secs=0.2` parameter requires 0.2s of sustained speech energy before the onset event fires. Combined with pipeline processing latency, the `VAD_STARTED` signal arrives ~66 frames (1.32s) into the capture phase. Any audio written to the ring between wake detection and `VAD_STARTED` is never read. In the confirming run (2026-04-03), a one-word utterance ("hello") was completely truncated — only the trailing vowel fragment and trailing silence reached Deepgram, returning confidence=0.000 and an empty transcript.

**Fix:** Always read the ring from `wake_pos` (the `write_pos` at the moment of `WAKE_DETECTED`) to `end_pos` (the `write_pos` at `VAD_STOPPED`). This captures the full utterance including any pre-speech audio. `VAD_STARTED` is retained as an advisory signal — logged as `vad_gap` to make onset latency visible — but not used to trim the span start.

**Forward design constraint:** VAD signals are advisory, not authoritative for span boundaries. Audio capture commences unconditionally on wake detection and ends on `VAD_STOPPED`. Future dictation mode will extend this further: the span end will also become advisory (configurable policy: VAD_STOPPED after N seconds, explicit command, or timeout) to support extended capture without the 1.8s `stop_secs` cutoff.

**Confirmed ring buffer health (same run):** `rms=168.6`, `zeros=0`, `stale=False` — the buffer writes and reads are mechanically correct. The truncation was purely a span-selection bug, not a buffer or transport issue.

## Two-Phase Shutdown Protocol (2026-04-03)

**Status: implemented and proven for Python-level ordering (2026-04-03, run 3). Kernel-level crash in driver teardown path remains open — see `shutdown_and_buffer_patterns.md` Root Cause 5.**

The two-process architecture isolates the recorder child from the master's cognitive loop, but SIGINT is delivered to the entire process group — both processes simultaneously. A naive SIGINT handler in the child that calls `task.cancel()` directly reproduces the same PortAudio/I2S race as the single-process crash (Root Cause 3/4 in `shutdown_and_buffer_patterns.md`).

**Resolution:** A unified `_initiate_shutdown()` coroutine in the child with a once-only guard. All three shutdown triggers (SIGINT, SIGTERM, SHUTDOWN pipe command) converge to this single path. The invariant is: `stop_stream()` completes before `task.cancel()` fires.

Two new pipe signals carry shutdown progress to the master:

| Signal | Meaning |
|---|---|
| `SHUTDOWN_COMMENCED` | Child has begun teardown — master exits its receive loop |
| `SHUTDOWN_FINISHED` | Child cleanup complete — master may safely unlink SharedMemory |

**Why SHUTDOWN_FINISHED matters:** SharedMemory is created and owned by the master. If the master unlinks it while the child is still in `shm.close()`, the child gets a use-after-free. The master's `shutdown_child()` drains the pipe waiting for SHUTDOWN_FINISHED (5s deadline) before proceeding with cleanup, escalating to SIGTERM then SIGKILL only if the deadline passes.

**SIGINT defer window:** `signal.signal(SIGINT, SIG_IGN)` at child process entry closes the narrow race between fork and `loop.add_signal_handler(SIGINT, ...)`. The event loop handler overrides it immediately on entry to `asyncio.run()`.

See `interface_spec.md` §3 Shutdown sequence for the full protocol diagram.

## Idle Phase: ONNX Off During Cognitive Loop (2026-04-03)

**Status: implemented and confirmed (2026-04-03, commit 140c9bd).**

The master previously sent `SET_WAKE_LISTEN` before entering the cognitive loop, causing OWW to run at full rate (~1 predict per 80ms, each 22–32ms) for the entire ~10s Claude window. Any wake detection during this window is discarded anyway (`processing=True` guard in master). This was pure CPU load with no benefit.

**Fix:** New `"idle"` RecorderState phase — stream active, ring buffer writes active, both OWW and Silero gated off. Duty cycle drops to baseline (~1.5ms mean, ~3ms max) during the cognitive loop.

**Phase flow:**
```
wake_listen → (WAKE_DETECTED) → capture → (VAD_STOPPED) → idle → (cognitive loop complete) → wake_listen
```

**Protocol:** Master sends `SET_IDLE` immediately on `VAD_STOPPED` (before reading ring buffer or starting STT). Master sends `SET_WAKE_LISTEN` in a `finally` block after `cognitive_loop()` returns — ensures transition happens even on exception.

**Gating mechanism:** No new processor logic required. OWW already gates on `state.wake_listen` (False during idle). Silero already gates on `state.capture` (False during idle). `AudioShmRingWriteProcessor` already gates on `not state.dormant` (idle is not dormant — writes continue).

**Confirmed (2026-04-03):** Cognitive loop duty cycle max: 3.0ms (vs 49.6ms before).

## OWW Predict Timing — Characterisation (2026-04-03)

**Measured with periodic predict window logging (`[OWW/N]`) introduced 2026-04-03.**

On Pi 4 Cortex-A72 with SCHED_FIFO priority 50, OWW predict via `asyncio.to_thread`:

| Window | Calls | Mean | p95 | Max |
|--------|-------|------|-----|-----|
| OWW/25 | 25 | 22.1ms | 22.8ms | 27.7ms |
| OWW/50 | 25 | 23.5ms | 24.7ms | 25.0ms |
| OWW/75 | 25 | 22.3ms | 23.2ms | 23.5ms |
| OWW/100| 25 | 25.3ms | 30.7ms | 35.4ms |
| OWW/125| 25 | 26.8ms | 27.8ms | 28.2ms |
| OWW/150| 25 | 29.0ms | 30.0ms | 30.2ms |
| OWW/175| 25 | 31.9ms | 32.7ms | 39.8ms |

Each call processes one 1280-sample (80ms) chunk. Calls fire every 4 audio frames.

**Upward trend:** Mean increases from 22ms to 32ms (+45%) over ~30 seconds. Likely thermal throttling (Pi 4 reduces from 1.5GHz to 1.0GHz as temperature rises). At 32ms mean, there is 48ms headroom before the next predict is awaited — still safe, but narrowing. If trend continues to ~80ms mean, the `await self._pending_predict` in `process_frame` would start blocking the pipeline.

**Observation:** predict runs consistently above the 20ms frame budget, but the pipeline tolerates this because predict fires only every 4 frames (80ms interval). The frame budget alarm is not triggered because duty cycle (bookend measurement) correctly shows low utilization — the `to_thread` offloads the cost off the event loop.

## TTS Playback Is Covered by the Idle-Phase Bracket (2026-04-04)

**Confirmed:** TTS playback (step 8) inherits OWW/Silero protection from the existing idle-phase protocol with no additional gates required.

`master_loop` sends `SET_IDLE` immediately on `VAD_STOPPED`, then calls `cognitive_loop()`. TTS runs inside `cognitive_loop`. `SET_WAKE_LISTEN` is sent in a `finally` block after `cognitive_loop()` returns. OWW inference gates on `state.wake_listen` (`recorder_child.py` line 466) — False throughout idle. The entire STT → agent → TTS span is bracketed.

**Cross-process ONNX concurrency (Piper + OWW):** During TTS playback, master runs Piper ONNX on cores 1–3 while the recorder child is in idle phase on core 0 (SCHED_FIFO). OWW is gated off in idle, so there is no concurrent ONNX within the recorder child. However, if a future change re-enables OWW during idle, cross-process ONNX would run simultaneously — separate processes with separate ONNX sessions on separate pinned cores. This has not been characterised on Pi 4; flag as a validation item on first TTS run (watch duty-cycle reports for thermal / cache-pressure effects on OWW predict timing).

## TTS Rearchitecture — Step 8/9 Prerequisite

**Status: evaluation and tuning complete (sessions 1–5, 2026-04-05). Ready for Phase 3 integrated test.**

**Root cause for replacement:** `PiperTTS` (EU-7, proven 2026-04-04) has two independent
failures on 1 GB Pi 4:

1. **OOM kill** — `en_US-lessac-medium` (~63 MB ONNX) exhausted total swap.
   master.py RSS 317 MB + 385 MB swap ≈ 700 MB against 900 MB total. Kernel sent SIGKILL.
2. **Audio tearing** — quality below threshold, observed before the kill.

Either condition independently blocks step 8 delivery.

**Interface contract:** `TTSBackend` ABC in `forked_assistant/src/tts.py`:
- `warm() -> None` — prime connection; call once during non-blocking window (STT/agent init)
- `play(Iterator[str]) -> None` — accepts sentence-aligned chunks; plays via pyalsaaudio ALSA hw:0,0; blocks until done
- `close() -> None` — release resources at process exit

Audio output uses `pyalsaaudio` (direct `snd_pcm_writei()`). PyAudio/PortAudio rejected:
PortAudio's callback thread gets descheduled on Pi 4 ARM causing hardware buffer underruns
(confirmed session 2, 2026-04-05).

**No process breakout needed:** Cloud TTS runs HTTP API calls in master process
(cores 1–3). No ONNX loaded on master. OWW/Silero are gated off during TTS (idle phase
bracket). Memory pressure from Piper ONNX is eliminated entirely.

**Platform precedence and selected voices (sessions 3–5, 2026-04-05):**

| Priority | Backend | Voice | Speed tiers (Short/Medium/Long) | Role |
|----------|---------|-------|---------------------------------|------|
| 1 | Cartesia | Allie (`2747b6cf-fa34-460c-97db-267566918881`) | 0.85 / 1.0 / 1.2 | Primary |
| 2 | ElevenLabs | Matilda (`XrExE9yKIg1WjnnlVkGX`) | 0.85 / 1.16 / 1.2 | Fallback |
| 3 | Deepgram | Helena (`aura-2-helena-en`) | 1.05 / 1.2 / 1.4 | Tertiary |

Defaults are wired into `tts.py` — `CartesiaTTS()` requires no arguments.

**Deepgram notes:** REST-only (full audio fetched before playback); starting-click
artifact unresolved; pronunciation control (inline IPA) available but deferred.
Unsuitable as primary due to latency scaling with content length.

**Evaluation folder:** `mvp-modules/archive/tts_evaluation/`
- `AGENTS.md` — evaluation guide, interface contract, session history
- `effort_log.md` — running session log with all measurements and decisions
- `voice_tuning_results.md` — final voice selections, speed tiers, platform precedence
- `deepgram_tts_notes.md` — Deepgram Aura API reference, pronunciation control syntax

---

## Why Recorder Is Capture-Only

The recorder child owns the microphone and nothing else. Playback (TTS) belongs to the master or a future separate process. This keeps the child simple and aligned with the ReSpeaker hat's input-focused design. It also avoids bidirectional audio I/O races in the same process.
