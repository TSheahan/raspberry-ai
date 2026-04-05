# TTS Evaluation — Session 3 Summary (2026-04-05)

## What happened

Phase 1 re-run and full Phase 2 comparison across all three TTS backends.
Audio tearing fix (pyalsaaudio) confirmed clean. Cartesia SDK v3 required
three rounds of debugging (API change, context_id bug, error handling).

## Results

| Backend | Avg first-chunk | Streaming | Quality | RSS |
|---------|-----------------|-----------|---------|-----|
| Deepgram | 2642ms | No (REST) | Outstanding | 54 MB |
| ElevenLabs | 486ms (warm) | Yes | Outstanding | 55 MB |
| Cartesia | 1043ms | Yes | Outstanding (rushed) | 57 MB |

All three produce outstanding audio via pyalsaaudio on Pi 3.5mm jack.
No OOM risk — all under 61 MB RSS. No tearing with pyalsaaudio.

ElevenLabs had a 2791ms cold-start on its first isolated call. In the
combined run (Deepgram first), the cold start disappeared — warm TCP/TLS.

## Cartesia SDK v3 bugs hit and fixed

1. **API change:** `websocket_connect()` is now a context manager.
   `send()` takes a dict, not keyword args. Iteration is over the
   connection, not the return of `send()`.

2. **context_id validation:** The SDK auto-generates a context_id with
   `::` suffix, which violates Cartesia's own rule (alphanumeric,
   underscores, hyphens only). Fix: supply explicit `"context_id": "eval"`.

3. **Error response structure:** The `error` field on error responses is
   `None`; the actual message is in the `message` field. The `model_dump()`
   call was needed to discover this — the Pydantic model has both fields.

## Observations

- **ALSA leading truncation:** Cartesia sentences 1 and 3 had ~100ms
  clipped at the start during live playback. WAV files confirmed clean
  via `aplay`. Cause: first `snd_pcm_writei()` on a freshly opened PCM
  device loses samples before hardware DMA buffer fills. Fix: silence
  pre-fill on first write.

- **ElevenLabs cold start:** First call 2791ms, subsequent calls 334–790ms.
  Server-side model loading for Flash v2.5. Mitigable with pre-warm call
  during STT phase.

- **Cartesia rushed pacing:** Katie speaks noticeably fast at default
  speed 1.0. Tunable via `generation_config.speed` (0.6–1.5).

- **Deepgram structural latency:** REST-only means full audio download
  before playback. `b"".join(response)` in current code. Progressive
  write or future WebSocket streaming would improve perceived latency.

## Decision

Keep all three as selectable `TTSBackend` modules. No single controlling
decision factor. Proceed to tuning phase.

## Code changes this session

| File | Change |
|------|--------|
| `compare_tts.py` | Cartesia v3 WebSocket API (context manager, dict send, connection iteration), explicit context_id, error handling with `message` field, model default sonic-english → sonic-3 |
| `tts.py` | Same Cartesia v3 fixes in production `CartesiaTTS`, model default update |
| `effort_log.md` | Deepgram re-capture results, ElevenLabs Phase 2a, Cartesia Phase 2b, combined run, comparison summary, decision |

## Commits

| Hash | Description |
|------|-------------|
| 039e5e2 | Fix Cartesia v3 SDK API; record Deepgram/ElevenLabs Phase 1-2 results |
| 88c3504–e120768 | Cartesia debugging: diagnostic logging, context_id fix, error handling |

## What's next (session 4 — on Pi)

Tuning sprint. See `tuning_plan.md`. Short sentences only for warm-start
testing — long sentences carry no additional signal.
