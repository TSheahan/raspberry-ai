# Forked Assistant Architecture

**Date:** 2026-04-01
**Status:** Design spec — not yet implemented
**Predecessor:** `step7_working/` (v01–v11, v10a)

---

## Problem Statement

Adding a Claude subprocess (7–12s CPU) to a working Pipecat voice pipeline on Pi 4 causes hard reboots. The failure mechanism is: unbounded Pipecat queues + CPU starvation during the cognitive loop + USB isochronous audio transfer starvation → kernel panic or USB host controller reset.

Logic reformulation within a single process (v01 through v10a) resolved all operational-path issues — wake detection, VAD, STT, Claude response — but the shutdown-path crash persists. The crash occurs in PortAudio's C layer during teardown when PyAudio's callback thread races against asyncio cancellation after a cognitive loop has run. This is beyond Python-level control.

## Solution: Two-Process Architecture

Break the voice pipeline into two OS processes, each pinned to a dedicated CPU core on the Pi 4's quad-core ARM.

### Recorder Child (Core 0, pinned)

Owns the microphone. Runs the proven Pipecat pipeline: `transport.input() → GatedVADProcessor → OpenWakeWordProcessor → AudioShmRingWriteProcessor`. Writes raw audio frames to shared memory continuously. Sends state-transition signals over a Unix domain socket pipe. Never imports Claude, Deepgram, or any heavy dependency.

The recorder child is specifically a **recorder**, not a general audio process. Playback (TTS) is scoped to the master process or a separate pinned process if needed. This aligns with the design pressure of the 4-mic ReSpeaker hat — the recorder child is tuned for capture.

### Master Process (Cores 1–3)

Receives signals from the recorder child. Reads audio from the shared ring buffer on demand. Runs the cognitive loop: Deepgram STT → Claude → response handling. Controls the recorder child's operating mode via command signals.

### Why This Works

| Single-process problem | Two-process resolution |
|---|---|
| CPU contention: Claude subprocess competes with ONNX inference | Processes on separate cores. No competition. |
| Queue growth: frames accumulate during cognitive loop | Recorder writes to ring buffer at constant rate regardless of master state. No queue. |
| USB audio starvation: isochronous transfers starved by CPU load | Recorder's dedicated core gives uninterrupted USB attention. |
| Shutdown race: PyAudio callback vs asyncio teardown | Master sends SHUTDOWN; recorder tears down on its own schedule. If hung, SIGKILL — a killed child doesn't reboot the Pi. |

---

## Design Principles

### Separate data plane from control plane

Audio data flows through shared memory (ring buffer) — high volume, continuous, zero-syscall writes. Control signals flow through a pipe — low volume, sporadic, immediate delivery. Mixing them in a single channel (as Pipecat's frame stream does internally) creates the access-pattern conflict that caused the original queue accumulation.

### VAD as sensor, not gate

The recorder child reports what it observes: wake word detected, speech started, speech stopped. The master decides what those observations mean for the current interaction mode. This decouples the audio sensing from the consumption policy, enabling:

- **Quick command mode:** VAD_STOPPED means "utterance complete, batch-transcribe"
- **Dictation mode:** ignore VAD_STOPPED unless silence exceeds N seconds, keep streaming
- **Future multi-turn:** use VAD_STOPPED to segment turns but don't stop the stream

### State object pattern (from v11)

Both processes use a centralized `State` object that owns all shared mutable state, exposes read-only properties, and executes side-effects on phase transitions. Processors/components hold only a reference to the state object, never to each other. Weak-ref pointers from the state object into controlled components prevent reference cycles.

This pattern was proven in v10→v11 within the single-process pipeline and carries forward directly.

---

## Key Technical Decisions

### Ring buffer via `multiprocessing.shared_memory.SharedMemory`

- Standard library, Python 3.8+. Uses `/dev/shm` (tmpfs) on Linux.
- Single-writer (recorder child) / single-reader (master) — no locks needed.
- Write cursor as monotonic uint64, read via aligned access on ARM64.
- 512KB ring = ~16 seconds of lookback at 16kHz int16 mono.
- Audio frames (640 bytes per 20ms) written by memcpy, sub-microsecond.

### Signals via `multiprocessing.Pipe()`

- Unix domain socket pair with pickle serialization.
- Message-oriented: each `send()`/`recv()` is one complete Python dict.
- Sub-millisecond latency. Kernel-buffered (~64KB).
- Selectable fd for integration with event loops.
- Bidirectional (duplex=True default).

### Process management via `multiprocessing.Process`

- Master spawns recorder child via fork.
- Core pinning via `os.sched_setaffinity()` immediately after fork.
- Master holds process handle for `.terminate()` / `.join()` on shutdown.
- Clean shutdown: master sends SHUTDOWN command, waits with timeout, escalates to SIGKILL.

### Playback scoped out of recorder child

Playback (TTS) belongs to the master process or a future separate pinned process. The recorder child is capture-only. This keeps the child simple and aligned with the ReSpeaker hat's input-focused design.

---

## Consolidated Learnings from Step 7

These learnings from v01–v11 and the crash analysis inform the two-process design.

### CPU budget and frame timing

- PyAudio callback produces one `InputAudioRawFrame` every **20ms** (hardware-paced, not governed by asyncio).
- Silero VAD ONNX inference: 15–25ms per frame.
- OpenWakeWord ONNX: 20–40ms per 80ms window (accumulates 4 frames / 1280 samples before predict).
- Both ONNX workloads must run in **non-overlapping phases**: OWW in WAKE_LISTEN, Silero in CAPTURE, neither during cognitive processing.
- The 20ms frame interval is the fundamental clock. Any per-frame work exceeding this budget causes queue growth.

### Pipecat queue architecture

- Every Pipecat pipeline level uses unbounded `asyncio.Queue` — no backpressure, no dropping, no bound.
- PyAudio's callback thread pushes frames via `asyncio.run_coroutine_threadsafe()`, which is not governed by queue fullness.
- When any processor falls behind, frames accumulate without limit.
- The ring buffer eliminates this: fixed-size, oldest data is overwritten, no growth.

### VAD controller wiring

- `VADController` event handlers (`on_speech_started`, `on_speech_stopped`, `on_push_frame`, `on_broadcast_frame`) are the **functional emission mechanism**, not optional observability.
- `on_speech_stopped` must `broadcast_frame(VADUserStoppedSpeakingFrame, ...)` — without it, the downstream capturer never triggers.
- Attribute initialization must precede handler registration (silent `AttributeError` in async handlers otherwise).
- Diagnostic prints in VAD handlers are essential — silent VAD failure is indistinguishable from "nobody is speaking."

### OWW state management

- OWW preprocessor has five internal buffers: `prediction_buffer`, `raw_data_buffer`, `melspectrogram_buffer`, `feature_buffer`, `accumulated_samples`.
- All must be reset on every ungating transition (CAPTURE→WAKE_LISTEN) or stale features combine with fresh audio to produce false-positive wake detections.
- Score threshold: 0.5. False positives observed at 0.865 before reset was implemented.

### Stream lifecycle

- `stop_stream()` / `start_stream()` must never be called synchronously from within a Pipecat frame-processing callback (PortAudio deadlock / USB fault on Pi).
- Silero LSTM hidden states must be reset **before** stream resume — ordering prevents stale state contamination.
- These concerns remain relevant inside the recorder child's Pipecat pipeline.

### Shutdown

- Ctrl+C before any cognitive loop exits cleanly; after is fatal (in single-process).
- The crash occurs after `CancelFrame` reaches the end of the pipeline — during PyAudio/PortAudio teardown.
- The two-process design eliminates this: the recorder child shuts down independently, and a killed child process doesn't take the Pi down.

---

## Success Criteria

1. Recorder child runs stably on a pinned core, cycling through DORMANT → WAKE_LISTEN → CAPTURE states on command.
2. Ring buffer provides continuous audio to master with zero-copy reads.
3. Master can batch-read a captured utterance span and dispatch to STT.
4. Master can tail the ring buffer for streaming STT (extensible, not required for first delivery).
5. Complete voice turn (wake → capture → STT → Claude → return to listening) across the process boundary.
6. Survive 3–5 consecutive turns without degradation.
7. Clean shutdown from any state — no Pi reboot under any normal condition.
8. Ctrl+C at any point in the turn cycle exits cleanly.
