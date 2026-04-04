# TTS Evaluation — Effort Log

## Session 2026-04-05 — Evaluation setup

### Context

PiperTTS (EU-7, proven 2026-04-04) is unsuitable for production on 1 GB Pi 4. Two
independent failure modes observed in the same run:

1. **OOM kill** — master.py RSS 317 MB + 385 MB swap = ~700 MB against 900 MB total
   swap. Kernel sent SIGKILL. Recorder child ran for ~4 min until Pipecat idle timeout.
2. **Audio tearing** — observed during playback before the kill. Quality is below
   acceptable threshold for intended use cases (CPAP check-in, calendar query, bedtime
   routine).

### Setup completed this session

- Abstract `TTSBackend` class broken out in `src/tts.py`; `PiperTTS` now inherits from it.
  The Piper synthesis path is disabled via a stub in `PiperTTS.play()` (was already
  stubbed after the OOM run — the stub is left in place; remove it if Piper is ever
  re-enabled on different hardware).
- `DeepgramTTS` skeleton added to `src/tts.py` — implements `TTSBackend`, uses
  `deepgram-sdk` (already in Pi venv), `linear16` encoding, per-chunk synthesis loop.
- Evaluation folder and AGENTS.md created.
- Deepgram TTS voice controls reference deposited in `deepgram_tts_notes.md`.
- Annotation added to `memory/architecture_decisions.md` routing to this folder.

### Open questions going into Phase 1

- **Deepgram linear16 sample rate:** Aura-2 likely returns 24000 Hz PCM. Confirm from
  response headers or SDK docs before opening PyAudio stream. Hard-coding 22050 (Piper
  rate) will cause chipmunk/slowed playback. The `DeepgramTTS` skeleton currently uses
  24000 as the default — verify on first run.
- **SDK `generate()` return type:** need to confirm whether `dg_client.speak.v1.audio.generate()`
  returns an iterable of bytes chunks (streaming) or a single bytes object. If iterable,
  write progressively to the PyAudio stream; if single bytes, write in one call. Inspect
  on first Pi run.
- **Encoding choice:** `linear16` (raw PCM S16_LE, no decode overhead) vs `mp3`
  (smaller payload, requires decode). Prefer `linear16` for Pi — avoid adding a decode
  dependency. Test `linear16` first.

---

## Phase 1 — Deepgram Aura (not yet run)

*Populate after Pi run.*

| Metric | Value |
|--------|-------|
| Test sentence | — |
| API call → first audio | — ms |
| Per 15-word sentence, wall time | — ms |
| RSS during synthesis | — MB |
| Audio quality (subjective) | — |
| OOM? | — |
| Proceed to Phase 3? | — |

---

## Phase 2 — Cartesia (not yet run, conditional)

*Run only if Phase 1 latency is marginal (> 800ms per sentence).*

---

## Phase 3 — Integrated pipeline test (not yet run)

*Populate after Pi run.*

| Metric | Value |
|--------|-------|
| VAD_STOPPED → first audio | — s |
| Multi-turn: turns without degradation | — |
| Clean Ctrl+C from wake_listen | — |
| RSS during full turn | — MB |

---

## Decision (not yet made)

*Record selected backend and rationale here.*
