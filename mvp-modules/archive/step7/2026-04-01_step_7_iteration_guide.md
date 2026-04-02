# Step 7 Iteration Guide — Agentic Layer (claude -p)

**Working directory:** `step7_working/` — versioned files `voice_pipeline_step7_v01.py`, `v02.py`, `v03.py` etc.
Observations logged in `step7_working/observations/`.



**Date:** 2026-03-31
**Script:** `voice_pipeline_step7.py`
**Builds on:** `voice_pipeline_step6.py` (step 6 — wake + capture + STT, delivered 2026-03-31)

---

## Goal

Add VAD-triggered end-of-utterance detection and the agentic layer (`claude -p`).
First full cognitive loop: speech in -> transcript -> Claude response text out.

---

## What changed from step 6

| Change | Why |
|--------|-----|
| Added `VADProcessor` with `SileroVADAnalyzer` | Auto-detect end of utterance (no Ctrl+C needed) |
| VAD `stop_secs=0.8` | 0.2s default too aggressive — normal speech pauses would cut off |
| `UtteranceCapturer` reacts to `VADUserStoppedSpeakingFrame` | Triggers cognitive loop when user stops speaking |
| Cognitive loop runs in `asyncio.create_task` | Pipeline keeps flowing (wake word stays responsive) |
| STT moved into `UtteranceCapturer._cognitive_loop` | No longer post-pipeline batch — runs inline after VAD stop |
| `run_claude()` subprocess call added | Option A from brief: `claude -p` locally on Pi |
| After cognitive loop completes, returns to wake word listening | Multi-turn ready |
| `self.processing` guard | Prevents overlapping cognitive loops if wake fires during processing |

## Pipeline order

```
transport.input() -> VADProcessor -> OpenWakeWordProcessor -> UtteranceCapturer -> (sink)
```

VAD goes first so `VADUserStoppedSpeakingFrame` propagates through to the capturer.
Wake word processor passes all frames through (including VAD frames).

## Key patterns preserved from step 6

- Every `FrameProcessor` subclass overrides `process_frame` and calls `push_frame`
- `list.append` + deferred `np.concatenate` (no `np.append` in hot path)
- `PipelineRunner` handles SIGINT — no custom signal handlers
- No manual `transport.cleanup()`

## Increment tracker

| # | Description | Status | Observation |
|---|-------------|--------|-------------|
| v1 | Full step 7 script | FAILED | Buffer overflow reboot on Ctrl+C after Claude call. Root cause: OpenWakeWordProcessor predict() runs during ~6.8s cognitive loop, CPU contention with claude subprocess causes chunk accumulation cascade. Clean exit when no Claude call was made. |
| v2 | Gate wake word predict during cognitive loop | FAILED | OWW gated but Silero VAD still ran ungated on every frame. Reboot on Ctrl+C during CAPTURING after a Claude call. VADProcessor is upstream and runs ONNX inference regardless of turn state. |
| v03 | Gate both VAD and OWW + full OWW reset on ungating | PARTIAL | OWW reset fixed the stale wake detection (attempt 5 shows no spurious wake). But Ctrl+C after a completed cognitive loop still reboots the Pi. Root cause is not OWW state — it's the shutdown race: PyAudio callback thread keeps firing during asyncio teardown. |
| v04 | Stop PyAudio stream before teardown + drain | TESTING | Patches input transport's cancel() to stop_stream() immediately on CancelFrame, before the normal cancel path runs. 100ms drain sleep lets queued frames propagate. Hypothesis: stopping frame production before asyncio teardown prevents the USB audio cascade. |

---

## Test procedure

```bash
source ~/pipecat-agent/venv/bin/activate
cd ~/pre-design-demos/e2e-pipeline-integration/step7_working
python voice_pipeline_step7_v04.py
```

1. Wait for "Listening for wake word..."
2. Say "hey Jarvis"
3. Expect "WAKE DETECTED" + "Listening for utterance..."
4. Ask a simple question (e.g. "What is the capital of France?")
5. Pause for ~1 second
6. Expect: VAD triggers, STT runs, Claude responds, latency printed
7. Say "hey Jarvis" again for a second turn
8. Ctrl+C to exit — expect clean shutdown

## What to watch for

- VAD fires too early (mid-sentence) — increase `stop_secs`
- VAD never fires — check Silero model loads, check audio frames reach VADProcessor
- Claude subprocess timeout — check `claude` CLI is authed on Pi
- Buffer overflow / reboot — should not happen (no `np.append` in hot path)
- Shutdown hang — check all processors have `process_frame` + `push_frame`
