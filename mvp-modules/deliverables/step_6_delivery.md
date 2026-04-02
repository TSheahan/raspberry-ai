# Step 6 Delivery — Wake Word + Utterance Capture + Deepgram STT

**Date:** 2026-03-31
**Deliverable:** `voice_pipeline_step6.py`
**Status:** Complete. All acceptance criteria met.
**Worklog:** `step6_worklog/` (full investigation history, test observations, iteration guide)

---

## What it does

Pipecat pipeline on Raspberry Pi 4 (`morpheus`):

```
[ReSpeaker mic] -> LocalAudioTransport -> OpenWakeWordProcessor -> UtteranceCapturer -> DeepgramSTTProcessor -> Sink
```

1. Listens continuously for "hey Jarvis" wake word
2. On detection, captures utterance audio into memory
3. On Ctrl+C, pipeline shuts down cleanly (<1s)
4. Post-pipeline, captured audio is batch-transcribed via Deepgram Nova-3
5. Process exits with code 0, no zombies

---

## Acceptance criteria

| Criterion | Result |
|-----------|--------|
| Wake word detection | "hey_jarvis" detected reliably at ~1m, score >0.5 |
| Audio capture | Continuous capture for 24+ seconds without corruption |
| STT transcription | Deepgram Nova-3 returns accurate transcript |
| Clean exit on Ctrl+C | Process terminates <1s after signal, exit code 0 |
| No zombie processes | Confirmed via `ps aux` |
| No buffer overflow on long utterances | 24s capture (counting 1-16) with no static or reboot |

---

## Bugs found and fixed

### 1. Shutdown hang — missing `process_frame` override

**Root cause:** Pipecat's `FrameProcessor.process_frame()` does **not** push frames downstream. It only performs internal lifecycle bookkeeping (setting `__started` on `StartFrame`, `_cancelling` on `CancelFrame`, etc.). It never calls `push_frame()`.

Any `FrameProcessor` subclass that omits a `process_frame` override silently swallows all frames — including `StartFrame` (so the pipeline never becomes ready) and `CancelFrame` (so Ctrl+C hangs until the ~20s timeout, then requires SIGTERM).

**The rule:** Every `FrameProcessor` subclass in a Pipecat pipeline must have:

```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    await super().process_frame(frame, direction)
    # ... any frame-specific logic ...
    await self.push_frame(frame, direction)
```

Even if the processor does nothing with most frames (like `DeepgramSTTProcessor`, which only acts at transcription time, not during frame flow), the override is required to keep the pipeline alive.

**How it was missed:** The original `first_attempt.py` had the override. During 8 rounds of troubleshooting the shutdown hang (v1-v8), increasingly complex workarounds were added (custom signal handlers, global shutdown events, force-closing PyAudio internals). v8 correctly cleaned up all the workaround jitter but accidentally also removed the `process_frame` override from `DeepgramSTTProcessor` — it looked like a no-op pass-through, so it was deleted. This was the only bug; all the workaround machinery was unnecessary.

**How it was found:** Incremental isolation. Starting from a working non-Pipecat baseline, Pipecat components were reintroduced one at a time. When the v7 increment (with `process_frame`) passed and v8b (identical minus `process_frame`) failed, the root cause was single-variable confirmed. Full methodology in `step6_worklog/step_6_shutdown_iteration_guide.md`.

**This is not documented in Pipecat.** The base class API is misleading — `process_frame` reads like a template method that handles propagation, but it requires the subclass to propagate explicitly.

### 2. Buffer overflow — O(n) array copy in audio path

**Root cause:** `np.append(buffer, chunk)` allocates a new array and copies the entire buffer on every audio chunk (~every 80ms). As the buffer grows, copy time approaches and then exceeds the chunk interval. On Pi 4 with USB audio (ReSpeaker), this cascades into ALSA underruns that can hang the USB audio device, requiring a reboot.

**The fix:** Replace `np.append` with `list.append(chunk)` (O(1) per chunk). Concatenate with `np.concatenate(chunks)` only at consumption time — once per `model.predict()` call in `OpenWakeWordProcessor`, and once at transcription time in `UtteranceCapturer.get_audio()`.

**Verified:** 24 seconds of continuous audio capture with no static, no corruption, no reboot. Previous versions consistently rebooted at ~8s+.

---

## Pipecat learnings for downstream steps

These apply to steps 7-9 and any future Pipecat pipeline work.

### Every FrameProcessor subclass must override `process_frame` and call `push_frame`

Non-negotiable. Even if the processor is a no-op for most frame types. Without this, `StartFrame` and `CancelFrame` are silently swallowed. The pipeline will appear to start (logs show linking) but never become ready, and Ctrl+C will hang.

### Let `PipelineRunner` handle SIGINT

Do not install custom signal handlers. Do not pass `handle_sigint=False`. Do not wrap `runner.run()` in `try/except KeyboardInterrupt`. Pipecat's built-in handler queues a `CancelFrame` that propagates through the pipeline cleanly — this is the intended shutdown path.

### Do not manually call `transport.cleanup()`

`CancelFrame` propagation triggers transport cleanup automatically. Manual cleanup in `finally` blocks double-cleans and can race with Pipecat's internal teardown.

### Post-pipeline work goes after `await runner.run(task)`

`runner.run()` blocks until the pipeline finishes (including clean CancelFrame propagation). Code after it runs in a fully shut-down pipeline. This is the correct place for final transcription, result logging, etc. — not inside `process_frame` and not in a signal handler.

### Avoid `np.append` in any frame-processing hot path

Any O(n)-per-chunk operation in `process_frame` will eventually cause frame backlog. On Pi 4 with USB audio, this manifests as ALSA underruns and USB device hangs. Use `list.append` + deferred `np.concatenate`.

### `RTVIProcessor` is harmless

`PipelineTask` auto-injects `RTVIProcessor` by default (`enable_rtvi=True`). It was a prime suspect for the shutdown hang but was cleared — `CancelFrame` propagates through it in ~2ms. No need to disable it unless there's a specific reason.

### `enable_rtvi=False` is available if needed

If `RTVIProcessor` causes issues in a future pipeline configuration, `PipelineTask(pipeline, enable_rtvi=False)` suppresses it. Not needed currently.

---

## What carries forward to step 7+

The pipeline structure (`LocalAudioTransport` -> processors -> `PipelineRunner`) is proven. Steps 7-9 from `starting_brief.md` (agentic layer, TTS, looping) add processors to this pipeline. The clean shutdown and buffer patterns carry forward unchanged.

For streaming dictation (long-form speech without Ctrl+C), the modification is to swap batch `transcribe_sync` for Deepgram's streaming/live WebSocket API, sending audio chunks as they arrive rather than accumulating and sending at end. The pipeline structure does not change.

---

## File inventory

```
e2e-pipeline-integration/
  INDEX.md                      # top-level roadmap
  starting_brief.md             # PoC brief (steps 1-9)
  step_6_delivery.md            # this document
  voice_pipeline_minimal.py     # step 4 deliverable
  voice_pipeline_step5.py       # step 5 deliverable
  voice_pipeline_step6.py       # step 6 deliverable
  step6_worklog/                # full investigation history
    README.md                   # guided tour
    step_6_progression_story.md # narrative of v1-v8 attempts
    step_6_shutdown_iteration_guide.md  # methodology, root cause, change audit
    observations/               # raw test output
    first_attempt.py            # original script
    diagnostic_minimal*.py      # non-Pipecat baseline series
    pipecat_shutdown_v*.py      # failed troubleshooting series
    incremental_v*.py           # systematic isolation series (v1-v10)
```
