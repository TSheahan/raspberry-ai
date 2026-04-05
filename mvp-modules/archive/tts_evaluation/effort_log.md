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

### Setup completed this session (2026-04-05 — session 1)

- Abstract `TTSBackend` class broken out in `src/tts.py`; `PiperTTS` now inherits from it.
  The Piper synthesis path is disabled via a stub in `PiperTTS.play()` (was already
  stubbed after the OOM run — the stub is left in place; remove it if Piper is ever
  re-enabled on different hardware).
- `DeepgramTTS` skeleton added to `src/tts.py` — implements `TTSBackend`, uses
  `deepgram-sdk` (already in Pi venv), `linear16` encoding, per-chunk synthesis loop.
- Evaluation folder and AGENTS.md created.
- Deepgram TTS voice controls reference deposited in `deepgram_tts_notes.md`.
- Annotation added to `memory/architecture_decisions.md` routing to this folder.

### Setup completed this session (2026-04-05 — sessions 1+2)

- `compare_tts.py` created in this folder — standalone comparison harness:
  - Plays each test sentence through Deepgram, ElevenLabs, Cartesia in sequence
  - Measures `latency_ms`, `total_ms`, `audio_kb`, `rss_mb` per sentence per backend
  - Run: `python mvp-modules/archive/tts_evaluation/compare_tts.py`
  - Deepgram-only run: add `--deepgram-only`
- `replay_wav.py` created — WAV replay diagnostic tool:
  - Three modes: PyAudio `frames_per_buffer` sweep, `--alsaaudio` (direct ALSA), `--aplay`
  - Used to isolate and confirm PortAudio as the tearing root cause (session 2)
- `CartesiaTTS(TTSBackend)` skeleton added to `src/tts.py`
- `ElevenLabsTTS(TTSBackend)` skeleton added to `src/tts.py`
- Audio output replaced: PyAudio → pyalsaaudio on Linux (session 2, see below)

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

## Phase 1 — Deepgram Aura (Pi run, session 1, PyAudio audio output)

Run from `~/deepgram-benchmark-venv` on morpheus. Audio output via PyAudio.

| Sentence | Latency | Total | Audio | RSS |
|----------|---------|-------|-------|-----|
| Hello, how can I help you today? | 2053ms | 4696ms | 118 KB | 65 MB |
| Your CPAP therapy last night... | 3016ms | 8397ms | 238 KB | 66 MB |
| I have added a reminder... | 2771ms | 7485ms | 208 KB | 67 MB |
| **avg** | **2613ms** | | | **66 MB** |

**Latency verdict:** MARGINAL against 800ms target. Structural — Deepgram TTS is
REST-only; full audio received before playback starts. ElevenLabs Flash v2.5
(~75ms first chunk) is the right comparison.

**Memory verdict:** 66 MB RSS — well clear of the 700 MB danger zone.

**Audio quality (PyAudio):** Tearing present on all runs. Content confirmed clean
via `aplay` — tearing is in the PortAudio path, not the synthesis.

### Phase 1 re-run needed (pyalsaaudio)

Session 2 replaced PyAudio with pyalsaaudio (tearing fix confirmed). Phase 1
must be re-run with the updated `compare_tts.py` to get a clean quality assessment.
Latency numbers are expected unchanged (API latency is independent of audio output).

---

## Phase 2 — ElevenLabs / Cartesia (not yet run, conditional)

*Run only if Phase 1 latency is marginal (> 800ms per sentence). Deepgram latency
was 2613ms avg — likely proceeding to Phase 2a (ElevenLabs) for streaming latency.*

---

## Audio Tearing Investigation (session 2, 2026-04-05)

### Root cause: confirmed

**PortAudio's internal callback thread** causes tearing on bcm2835 (Pi 4 headphone
jack). Even for "blocking" `write()`, PortAudio interposes a background thread
between the user's write call and the ALSA hardware. When that thread gets
descheduled on the Pi 4's ARM cores, the hardware buffer drains → audible glitch.

### Evidence

Three-way replay comparison using `replay_wav.py` with `deepgram_00.wav`:

| Mode | Result |
|------|--------|
| `--aplay` (subprocess pipe to `aplay -D hw:0,0`) | Clean — consistent across multiple cycles |
| `--alsaaudio` (pyalsaaudio, direct `snd_pcm_writei()`) | Clean — consistent across multiple cycles |
| `--frames 4096` (PyAudio, `frames_per_buffer=4096`) | Tearing — reduced vs 256 but still present |

PyAudio `frames_per_buffer` sweep (256 → 8192) showed tearing reduced at larger
values but never eliminated. This rules out period size as root cause and confirms
the PortAudio callback thread scheduling stall hypothesis.

### Fix applied

All audio output in `tts.py` and `compare_tts.py` now uses `pyalsaaudio` on Linux
via the `_AudioOut` abstraction. PyAudio retained as Windows-only fallback for dev.
`pyalsaaudio` dependency profiled in `profiling-pi/venv.md`.

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
