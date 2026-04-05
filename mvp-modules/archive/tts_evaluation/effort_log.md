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

### Phase 1 re-run (pyalsaaudio) — session 3, 2026-04-05

Run from `~/deepgram-benchmark-venv` on morpheus. Audio output via pyalsaaudio
(direct ALSA `hw:0,0`). No tearing observed — audio played clean through 3.5mm jack.

| Sentence | Latency | Total | Audio | RSS |
|----------|---------|-------|-------|-----|
| Hello, how can I help you today? | 1901ms | 4192ms | 106.9 KB | 47 MB |
| Your CPAP therapy last night... | 3230ms | 8845ms | 262.5 KB | 48 MB |
| I have added a reminder... | 2869ms | 7557ms | 219.4 KB | 49 MB |
| **avg** | **2667ms** | | | **48 MB** |

**Latency verdict:** MARGINAL (2667ms avg, threshold 800ms). Structural — Deepgram
TTS is REST-only; full audio downloaded before playback starts. Proceed to Phase 2.

**Memory verdict:** 48 MB RSS — well clear of the 700 MB danger zone.

**Audio quality (pyalsaaudio):** Clean — no tearing. Deepgram Aura-2 voice quality
is acceptable for CPAP/calendar/routine use cases.

**OOM?** No. 48 MB RSS is negligible.

---

## Phase 2a — ElevenLabs (Pi run, session 3, 2026-04-05)

Run from `~/deepgram-benchmark-venv` on morpheus. ElevenLabs starter plan subscription
active. Audio output via pyalsaaudio.

| Sentence | Latency | Total | Audio | RSS |
|----------|---------|-------|-------|-----|
| Hello, how can I help you today? | 2791ms | 4886ms | 98.0 KB | 49 MB |
| Your CPAP therapy last night... | 361ms | 5214ms | 226.4 KB | 50 MB |
| I have added a reminder... | 696ms | 4464ms | 176.3 KB | 50 MB |
| **avg** | **1283ms** | | | **50 MB** |

**Latency verdict:** First sentence 2791ms (cold start — connection establishment +
model load). Subsequent sentences 361ms and 696ms — well within 800ms target once
warm. Average pulled up by cold start; steady-state latency ~530ms.

**Memory verdict:** 50 MB RSS — well clear of danger zone.

**Audio quality:** [fill in — subjective assessment from Pi run]

**OOM?** No. 50 MB RSS is negligible.

**Cold start observation:** The 2791ms first-call latency is likely ElevenLabs
server-side model loading for Flash v2.5. In production, `agent.prepare()` could
pre-warm the connection by sending a dummy request during the STT phase.

---

## Phase 2b — Cartesia (Pi run, session 3, 2026-04-05)

Voice selected: Katie (`f786b574-daa5-4673-aa0c-cbe3e8534c02`) from
[play.cartesia.ai](https://play.cartesia.ai/voices?language=en).

Package: `cartesia==3.0.2`. SDK v3 broke the WebSocket API — `websocket_connect()`
now returns a context manager, `send()` takes a dict, iteration is over the
connection object. Additionally, auto-generated `context_id` contained `::` which
violates Cartesia's own validation; fixed by supplying explicit `context_id`.

| Sentence | Latency | Total | Audio | RSS |
|----------|---------|-------|-------|-----|
| Hello, how can I help you today? | 1184ms | 2571ms | 64.0 KB | 48 MB |
| Your CPAP therapy last night... | 857ms | 5021ms | 184.0 KB | 51 MB |
| I have added a reminder... | 1031ms | 4044ms | 134.0 KB | 52 MB |
| **avg** | **1024ms** | | | **50 MB** |

**Latency verdict:** 1024ms avg first-chunk (streaming). Above 800ms target but
audio begins playing while synthesis continues — perceived latency lower than
Deepgram REST (which blocks until full download).

**Memory verdict:** 50 MB RSS — well clear of danger zone.

**Audio quality:** Leading playback truncation on sentences 1 and 3 (first ~100ms
of audio clipped). WAV files confirmed clean via `aplay -D plughw:0,0` — the
truncation is in the ALSA device startup path, not the synthesis. Likely cause:
first `snd_pcm_writei()` on a freshly opened PCM device loses samples before the
hardware DMA buffer fills. A silence pre-fill on device open would fix this.

**OOM?** No. 50 MB RSS is negligible.

---

## Combined run — all three backends, session 3

Head-to-head with all backends in sequence per sentence. ElevenLabs warm by the
time it runs (Deepgram goes first). pyalsaaudio audio output throughout.

| Sentence | Backend | Latency | Total | Audio | RSS |
|----------|---------|---------|-------|-------|-----|
| Hello... | deepgram | 1845ms | 4018ms | 101.3 KB | 47 MB |
| Hello... | elevenlabs | 790ms | 2711ms | 89.3 KB | 51 MB |
| Hello... | cartesia | 1171ms | 2925ms | 80.0 KB | 54 MB |
| CPAP therapy... | deepgram | 3488ms | 9498ms | 281.3 KB | 55 MB |
| CPAP therapy... | elevenlabs | 334ms | 5185ms | 226.4 KB | 56 MB |
| CPAP therapy... | cartesia | 1085ms | 5323ms | 182.0 KB | 58 MB |
| Reminder... | deepgram | 2591ms | 6921ms | 202.5 KB | 59 MB |
| Reminder... | elevenlabs | 333ms | 4435ms | 191.6 KB | 59 MB |
| Reminder... | cartesia | 872ms | 4353ms | 154.0 KB | 61 MB |

### Summary

| Backend | Avg first-chunk | Streaming? | Avg total | RSS | Quality |
|---------|-----------------|------------|-----------|-----|---------|
| Deepgram | 2642ms | No (REST) | 6812ms | 54 MB | Outstanding |
| ElevenLabs | 486ms | Yes | 4110ms | 55 MB | Outstanding |
| Cartesia | 1043ms | Yes | 4200ms | 57 MB | Outstanding (rushed pacing — tunable via voice/speed) |

All three backends produce outstanding audio quality through the Pi 3.5mm jack
with pyalsaaudio output. No OOM risk — all under 61 MB RSS.

**ElevenLabs** is the clear latency winner: 486ms avg first-chunk, 5x faster
than Deepgram REST and 2x faster than Cartesia. The ~2.8s cold start observed
in isolated runs disappears when preceded by other network activity (warm TCP/TLS).

Cartesia's rushed pacing is likely tunable via `generation_config.speed` or
voice selection. Deepgram is structurally limited by REST-only (no streaming).

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

## Decision

**Keep all three as selectable `TTSBackend` modules.** Quality is outstanding
across the board. No single controlling decision factor — latency favours
ElevenLabs, simplicity favours Deepgram, Cartesia offers the richest control
surface. Subscription surprises or other contingencies could force a change.

Proceed to tuning phase: voice selection, cadence control, warm-start latency
optimisation across all three backends. `compare_tts.py` is the sandbox.
See `tuning_plan.md` for the spec.

---

## Session 5 — Voice audition and speed tier review (2026-04-05)

### Cartesia voice audition

Shortlist recovered after crash: Katie, Allie, Kayla. Voice IDs retrieved via
Cartesia API (`voices.list()`).

| Voice | ID |
|-------|----|
| Katie — Friendly Fixer | `f786b574-daa5-4673-aa0c-cbe3e8534c02` |
| Allie — Natural Conversationalist | `2747b6cf-fa34-460c-97db-267566918881` |
| Kayla — Easygoing Pal | `1ac31ebd-9113-405b-9d80-4a4bbbeea91c` |

Round 1 — short sentence at speed 0.85 (all three):

| Voice | First chunk | Audio |
|-------|-------------|-------|
| Katie | 1224ms | 98 KB |
| Allie | 1207ms | 94 KB |
| Kayla | 1064ms | 104 KB |

Round 2 — bagel monologue at speed 1.0 (all three). Kayla eliminated.

Round 3 — bagel monologue at speed 0.85 (Katie vs Allie):

| Voice | First chunk | Audio |
|-------|-------------|-------|
| Katie | 1327ms | 870 KB |
| Allie | 1263ms | 1080 KB |

**Selected: Allie.** Friendly diction; naturally slower base cadence than Katie
(more audio at same speed setting — gives ellipses and rhythm more space).
Katie retained as runner-up.

### ElevenLabs voice model review

Re-ran shortlist (Matilda, Rachel, Jessica, Sarah) plus Cartesia/Allie as
reference, short sentence at 0.85. No changes — Matilda confirmed as selection.
Rachel retained as runner-up.

### Speed tier review and cross-platform consistency check

Tier nomenclature standardised to Short / Medium / Long (was Default/Bulk/Heavy bulk
and Default/Mid/Fallback). Fallback demoted to inline annotation on a tier.

Cross-platform consistency verified by listening (Short sentence, Medium bagel quote,
Long bagel quote) on Cartesia/Allie and ElevenLabs/Matilda in sequence:

| Tier | Cartesia speed | ElevenLabs speed | Verdict |
|------|----------------|------------------|---------|
| Short | 0.85 | 0.85 | Consistent ✓ |
| Medium | 1.0 | 1.16 | Consistent ✓ |
| Long | 1.2 | 1.2 | Consistent ✓ |

Note: ElevenLabs Medium sits at 1.16 vs Cartesia 1.0 because Matilda's voice character
reads as more relaxed at equivalent speed values — the perceived pace matches.
ElevenLabs Long aligned to 1.2 (was previously labelled "API ceiling only" — confirmed
as the correct Long tier value, same as Cartesia).

### Platform precedence established

| Priority | Backend | Voice | Role |
|----------|---------|-------|------|
| 1 | Cartesia | Allie | Primary |
| 2 | ElevenLabs | Matilda | Fallback |
| 3 | Deepgram | Helena | Tertiary |

Deepgram speed tiers carried from Thalia evaluation; not re-validated with Helena
(P3 — effort stops here). Pronunciation control (inline IPA) annotated and deferred.

### Code changes

| File | Change |
|------|--------|
| `src/tts.py` | Module docstring updated with precedence order. `CartesiaTTS`: `_DEFAULT_VOICE_ID` = Allie, `_DEFAULT_SPEED` = 0.85, constructor defaults set. `ElevenLabsTTS`: `_DEFAULT_VOICE_ID` = Matilda. `DeepgramTTS`: `_DEFAULT_MODEL` = Helena, `_DEFAULT_SPEED` = 1.05. Section headers relabelled PRIMARY / FALLBACK / TERTIARY. |
| `voice_tuning_results.md` | Platform precedence table added. Actionable settings expanded. Speed tiers renamed Short/Medium/Long. Deepgram pronunciation annotation added. Cartesia section header corrected to Allie. |
| `voice_tuning_brief.md` | Voice IDs table expanded with all Cartesia shortlist + selection markers. |
| `AGENTS.md` | Voice audition phase marked complete. Final selections and precedence recorded. |
