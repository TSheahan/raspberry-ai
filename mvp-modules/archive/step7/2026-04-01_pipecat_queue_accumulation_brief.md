# Brief — Pipecat Queue Accumulation on Pi 4

**Date:** 2026-04-01
**Context:** Step 7 (agentic layer) of the e2e pipeline integration PoC
**Purpose:** Source material. Captures the problem space, prior learnings, and success criteria. Does not prescribe a solution.

---

## The problem

The Pipecat process on morpheus (Pi 4) cannot survive a complete voice turn (wake → capture → STT → Claude → return to listening) without rebooting on the next Ctrl+C or, in some configurations, immediately after the cognitive loop completes.

The pipeline functions correctly through each phase in isolation. Prior steps have demonstrated stable, repeatable operation of wake word detection (steps 4-6), utterance capture with VAD (steps 5-6), and STT transcription (step 6) — all running indefinitely with clean shutdown. The instability appears only when a long-running cognitive loop (7-12 seconds for STT + Claude subprocess) is introduced in step 7.

---

## The mechanism

### Frame production

PyAudio's callback thread produces one `InputAudioRawFrame` every **20ms** (hardcoded in pipecat's `LocalAudioInputTransport`: `frames_per_buffer = (sample_rate / 100) * 2`). This thread is driven by the ALSA/USB audio subsystem and is not governed by the asyncio event loop. It pushes frames via `asyncio.run_coroutine_threadsafe()`.

### Queue architecture

Pipecat uses **unbounded `asyncio.Queue` instances at every level** — no backpressure, no frame dropping, no bound:

- `BaseInputTransport` audio queue
- `FrameProcessor.__input_queue` (priority queue, per processor)
- `FrameProcessor.__process_queue` (per processor)
- `PipelineTask._push_queue`

When any processor's `process_frame` takes longer than the frame interval, frames accumulate without limit.

### Per-frame CPU budget

At 20ms per frame, the full processor chain must complete within 20ms to keep up with production. On Pi 4:

| Processor | Phase active | Approx cost per invocation |
|-----------|-------------|---------------------------|
| GatedVADProcessor | CAPTURING only | 15-25ms (Silero ONNX) |
| OpenWakeWordProcessor | LISTENING only | 20-40ms per predict (every 4th frame, i.e. per 80ms of audio) |
| UtteranceCapturer | CAPTURING only | <1ms (list.append) |

During LISTENING, OWW predict runs every ~80ms (it accumulates 4 frames of 320 samples to reach its 1280-sample chunk size). If a single predict takes >80ms, the queue begins growing. On Pi 4 Cortex-A72, OWW predict cost is variable and occasionally exceeds this budget.

### The 20ms / 80ms distinction

- **20ms**: the hardware-driven frame interval. PyAudio callback fires this often. This is the rate at which the asyncio queues receive new items.
- **80ms**: the OWW processing cycle. OWW needs 1280 samples (80ms at 16kHz) per predict call. So OWW runs every 4th frame. Its budget is 80ms, not 20ms — but only if the intervening 3 frames pass through cheaply.

Both numbers are relevant. The 20ms interval determines queue growth rate. The 80ms interval determines the OWW processing budget.

### The reboot mechanism

On Pi 4 with USB audio (ReSpeaker 4-Mic Array), CPU starvation cascades into the USB isochronous transfer layer. When the asyncio event loop cannot service PyAudio's callback thread promptly, ALSA underruns accumulate, the USB audio driver loses synchronisation, and the kernel either panics or the USB subsystem hangs — requiring hard reboot. This is not a software crash; the SSH session reports "Connection reset" because the device itself goes down.

---

## What we've learned (chronological)

### Step 6 — two buffer-related crashes

1. **Missing `process_frame` override**: a FrameProcessor subclass without `process_frame` silently blocked all downstream frames, causing shutdown to hang and the queue to grow on CancelFrame.
2. **O(n) `np.append` in hot path**: quadratic memory/CPU growth during audio capture. Fixed by switching to `list.append` + deferred `np.concatenate`.

Both confirmed the same underlying dynamic: any frame-processing delay on Pi 4 is eventually fatal.

### Step 7 v01 — Claude subprocess CPU contention

OWW `predict()` continued running during the 7-12s cognitive loop. The Claude subprocess consumed significant CPU, and OWW's ONNX inference competed for the remaining cycles. Frames accumulated, and Ctrl+C triggered the reboot.

### Step 7 v02 — OWW gated but VAD ungated

Gated OWW during cognitive loop, but Silero VAD (upstream in the pipeline) continued running ONNX inference on every frame. Two concurrent ONNX workloads during CAPTURING exceeded the CPU budget.

### Step 7 v02 (attempt 4) — stale OWW state

After gating resolved the CPU contention issue, a new failure: OWW's preprocessor feature buffers (melspectrogram, embeddings) survived the gating period. When gating lifted, stale features combined with fresh audio to produce a false wake detection (score 0.865) without any wake word spoken. Pipeline entered CAPTURING with nobody speaking, user hit Ctrl+C, reboot.

### Step 7 v03 — full OWW reset, still crashes

Full model state reset (prediction buffer + preprocessor raw_data_buffer, melspectrogram_buffer, feature_buffer, accumulated_samples) eliminated the false wake detection. But Ctrl+C after a completed cognitive loop still causes a reboot (attempt 5). The OWW reset resolved the state corruption problem but did not resolve the underlying accumulation/shutdown problem.

### Key pattern across all crashes

Every crash shares the same signature: `client_loop: send disconnect: Connection reset` after Ctrl+C, following a period where the cognitive loop ran. Ctrl+C before any cognitive loop (attempt 2) exits cleanly. Ctrl+C during initial LISTENING (before any wake word, all prior steps) exits cleanly. Something about having run a cognitive loop makes subsequent shutdown fatal.

---

## What is known to work

Steps 1-6 demonstrate that the following operations are individually stable on Pi 4 under Pipecat:

- **OWW wake word detection**: runs indefinitely in LISTENING, with predict every 80ms, clean Ctrl+C shutdown. Demonstrated across steps 4-6 and standalone wake word demos.
- **Silero VAD**: runs during CAPTURING, detects speech boundaries reliably. Demonstrated in steps 5-6.
- **Deepgram STT**: cloud call, minimal CPU. Demonstrated in step 6.
- **Audio capture with `list.append`**: no buffer growth issues. Demonstrated in steps 5-6.
- **Clean shutdown via PipelineRunner SIGINT**: works when no cognitive loop has run.

The components are proven. The instability emerges from the interaction between the cognitive loop's resource footprint and the pipeline's continued operation during and after that loop.

---

## Definition of success

The Pipecat process must be able to:

1. **Complete a full voice turn** — wake → capture → STT → Claude → return to listening — and remain in a healthy state afterwards.
2. **Survive multiple consecutive turns** — at least 3-5 turns without degradation.
3. **Shut down cleanly on Ctrl+C** at any point in the turn cycle, including after completed cognitive loops.
4. **Not reboot the Pi** under any normal operating condition, including rapid successive wake words, long Claude responses, or empty transcripts.

The process does not need to support barge-in, concurrent I/O in both directions, or sub-second shutdown. It needs to run reliably.

---

## Open questions for solution design

- Is the crash on post-loop Ctrl+C caused by residual state from `asyncio.create_task` / `asyncio.to_thread`, or by queue depth accumulated during the loop?
- Would explicitly stopping the PyAudio stream before asyncio teardown prevent the USB cascade?
- Is the cognitive loop's CPU footprint (subprocess + asyncio.to_thread) leaving background pressure that makes OWW's resumed predict calls slower than they were pre-loop?
- Would a bounded queue with frame-dropping at the transport level (patching `LocalAudioInputTransport`) address the root cause, or just mask it?
- Is there a simpler architecture where the audio stream is paused/stopped during the cognitive loop and restarted after, avoiding the queue accumulation problem entirely?
