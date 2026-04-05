# TTS Evaluation — Session 4 Summary (2026-04-05)

## What happened

Tuning sprint. Wired tuning controls for all three backends into
`compare_tts.py`, ran cadence and warm-start experiments on Pi, fixed
ALSA leading truncation, propagated settings into production classes,
added `warm()` interface to `TTSBackend`.

## Results

### Cartesia cadence (Katie, speed sweep)

| Speed | First chunk | Audio size | Subjective |
|-------|------------|------------|------------|
| 0.9   | 1351ms | 80 KB | (not selected) |
| 0.85  | 1204ms | 88–100 KB | "good" — user confirmed |
| 0.8   | 1182ms | 102 KB | (not selected) |

**Decision:** speed 0.85 for Katie (user confirmed after re-listen of all three).

### Warm-start back-to-back ("Hello.", 0.5s pause, 3 calls)

| Backend | Call 1 | Call 2 | Call 3 | Avg |
|---------|--------|--------|--------|-----|
| ElevenLabs | 318ms | 331ms | 452ms | 367ms |
| Cartesia | 1258ms | 1011ms | 1001ms | 1090ms |
| Deepgram | 1338ms | 1000ms | 1105ms | 1148ms |

No cold-start recurrence at 0.5s intervals for any backend.
ElevenLabs remains the latency champion at ~367ms warm average.

### ALSA leading-truncation fix

One period of silence (4096 samples × 2 bytes) written on first
`_AudioOut.write()` call. Confirmed clean — no leading truncation
on any of the six Cartesia test runs or ElevenLabs/Deepgram runs.

### ElevenLabs optimize_streaming_latency

Tested with `optimize_streaming_latency=2`: 668ms first chunk (warm).
Same ballpark as session 3 warm numbers. Param wiring confirmed working.

## Code changes

| File | Change |
|------|--------|
| `compare_tts.py` | CLI args: `--cartesia-speed`, `--cartesia-emotion`, `--cartesia-buffer-delay`, `--el-speed`, `--el-stability`, `--el-similarity`, `--el-optimize-latency`. Plumbed to API calls. ALSA silence pre-fill. |
| `src/tts.py` | `TTSBackend.warm()` — ABC method with default no-op. `DeepgramTTS.warm()`, `CartesiaTTS.warm()`, `ElevenLabsTTS.warm()` — throwaway synthesis to prime connection. `CartesiaTTS.__init__` accepts speed/emotion. `ElevenLabsTTS.__init__` accepts voice_settings/optimize_streaming_latency. ALSA silence pre-fill. |
| `AGENTS.md` | Interface contract updated with `warm()`. Phase 3 updated. |
| `tuning_plan.md` | Marked complete with outcome summary. |

## Not yet done (tuning plan items deferred)

- Idle-gap warm-start test (does cold start recur after 30s idle?)
- Cartesia single-connection multi-sentence (persistent WebSocket)
- Cartesia emotion tuning (only speed tested)

These are not blocking. Idle-gap testing belongs in Phase 3 integrated
test. Persistent WebSocket is an optimisation for later if Cartesia is
selected as primary.

## What's next (Phase 3 — integrated test)

1. Wire `tts.warm()` call into `master.py` during non-blocking window
2. Replace `PiperTTS(...)` with selected backend
3. Full cognitive turn on Pi: wake → STT → agent → TTS → wake_listen
4. Measure warm() vs no-warm() first-turn latency delta
5. Confirm: time-to-first-audio < 2s, no OOM, clean Ctrl+C, multi-turn
