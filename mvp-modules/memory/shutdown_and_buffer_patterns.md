# Shutdown and Buffer Patterns

Proven root causes, fixes, and anti-patterns for audio pipeline stability on Raspberry Pi 4. Extracted from step 6 (buffer overflow) and step 7 (shutdown crash) investigations.

## The 20ms Frame Clock

PyAudio's callback thread produces one `InputAudioRawFrame` every **20ms**, driven by the ALSA/USB audio subsystem. This is hardware-paced and not governed by the asyncio event loop. Frames are pushed via `asyncio.run_coroutine_threadsafe()`.

Any per-frame work exceeding 20ms causes queue growth. Pipecat uses unbounded `asyncio.Queue` at every pipeline level — no backpressure, no dropping, no bound. Growth is silent until it cascades.

## Root Cause 1: Buffer Overflow from O(n) Array Copy

`np.append(buffer, chunk)` allocates a new array and copies the entire buffer on every audio chunk (~every 80ms for OWW). As the buffer grows, copy time approaches and then exceeds the chunk interval. On Pi 4 with USB audio (ReSpeaker), this cascades into ALSA underruns that can hang the USB audio device, requiring a reboot.

**Fix:** `list.append(chunk)` for accumulation (O(1) per chunk). `np.concatenate(chunks)` only at consumption time — once per `model.predict()` call, once at transcription time. Never in the hot path.

**Applies to:** Any audio accumulation in `process_frame` or callback paths.

## Root Cause 2: CPU Starvation → USB Audio Cascade → Reboot

When the Claude subprocess runs (7–12s), it consumes significant CPU. Any ONNX inference still running during that window competes for remaining cycles. Frames accumulate in unbounded queues. On Ctrl+C, Pipecat attempts to flush queues while PyAudio's callback thread keeps firing and the USB isochronous transfer layer is starved. The kernel panics or the USB host controller resets. Hard reboot.

**Fix (single-process):** Phase-gate all ONNX workloads:
- **LISTENING:** OWW runs, Silero does not
- **CAPTURING:** Silero runs, OWW does not
- **PROCESSING (cognitive loop):** neither runs

**Fix (two-process, current architecture):** Recorder child on dedicated core 0. Master on cores 1–3. No CPU competition. Ring buffer replaces unbounded queues.

## Root Cause 3: Shutdown Race (Single-Process, Unresolvable)

After a cognitive loop has run, Ctrl+C triggers a race between PyAudio's callback thread and asyncio's cancellation path. The crash occurs in PortAudio's C layer during teardown — beyond Python-level control.

**Symptom:** Ctrl+C before any cognitive loop always exits cleanly. After a cognitive loop, Ctrl+C causes `client_loop: send disconnect: Connection reset` (Pi reboots).

**Mitigation (single-process):** Pause PyAudio stream during cognitive loop. Stop stream on `CancelFrame` before normal cancel path runs. These reduce the window but do not eliminate the race.

**Resolution:** Two-process architecture. Master sends SHUTDOWN command; recorder child tears down on its own schedule. If hung, SIGKILL — a killed child process doesn't reboot the Pi.

## OWW State Reset Protocol

OpenWakeWord's preprocessor accumulates state in five internal buffers: `prediction_buffer`, `raw_data_buffer`, `melspectrogram_buffer`, `feature_buffer`, `accumulated_samples`. All must be reset on every ungating transition (e.g., CAPTURE→WAKE_LISTEN).

Without reset, stale features combine with fresh audio to produce false-positive wake detections. Observed false positive score: 0.865 (threshold: 0.5).

## Silero LSTM Reset Ordering

Silero VAD maintains LSTM hidden states across frames. When resuming audio processing after a pause (e.g., after cognitive loop completes), reset hidden states **before** the first new frame arrives. If the stream resumes before the reset, stale hidden states contaminate the initial speech detection.

Implementation: `asyncio.create_task()` the reset-then-resume sequence, never synchronous from a Pipecat callback.

## Stream Lifecycle Anti-Patterns

| Anti-pattern | Consequence |
|---|---|
| Calling `stop_stream()`/`start_stream()` synchronously from `process_frame` | PortAudio deadlock or USB fault |
| Manual `transport.cleanup()` in `finally` blocks | Double-cleanup races with Pipecat teardown |
| Custom SIGINT handler | Bypasses Pipecat's CancelFrame propagation |
| `np.append` in any frame-processing hot path | O(n) copy → ALSA underrun → USB hang → reboot |
| Unbounded queue without backpressure | Silent frame accumulation until cascade |
