# TTS Evaluation — Session 1 Summary (2026-04-05)

## What Was Built

### `compare_tts.py` (new)
Standalone comparison harness in this folder. Plays each test sentence through
Deepgram, ElevenLabs, and Cartesia in sequence. Measures per-sentence:
- `latency_ms` — API call to first audio byte received (REST: full round-trip;
  streaming: first chunk)
- `total_ms` — API call to last byte played through PyAudio
- `audio_kb` — raw PCM bytes received
- `rss_mb` — process RSS from `/proc/self/status`

Flags: `--deepgram-only`, `--elevenlabs-only`, `--cartesia-only` (mutually
exclusive); `--save-wav DIR` saves each sentence as a properly headered WAV
file and prints `aplay` commands.

Platform note: `PYAUDIO_DEVICE_INDEX = 0` on Linux (bcm2835 ALSA device 0),
`None` (PortAudio default) on Windows. Unicode box-drawing replaced with ASCII
for Windows CP1252 compatibility.

### `tts.py` additions
- `ElevenLabsTTS(TTSBackend)` — `convert_as_stream()` → corrected to `stream()`
  after SDK introspection; `pcm_24000` output matches Deepgram sample rate
- `CartesiaTTS(TTSBackend)` — `websocket()` → corrected to `websocket_connect()`
  (3.x deprecation); `send()` signature unchanged

### `.env.example` (new at repo root)
Template with `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` (commented),
`CARTESIA_API_KEY` (commented), and agent subprocess vars.

### `.env` (at `C:\Users\tim\PycharmProjects\.env`, above repo root)
Sits above the repo so it is never inside any checkout. `find_dotenv()` (called
by `load_dotenv(override=True)`) walks upward from cwd and finds it. All scripts
already used `load_dotenv(override=True)` with no path — no code change needed.
Pi equivalent: place at the directory above the repo checkout.

### AGENTS.md updates
- End state item 3 reworded: PiperTTS retired — ported to TTSBackend, proven
  on Pi, synthesis stub left in place as record of rejection
- ElevenLabs added as Phase 2a candidate (account created, key needed on Pi)
- Cartesia demoted to Phase 2b (key already in `.env` from prior project)
- Evaluation sequence updated; file layout updated to include `compare_tts.py`

---

## SDK Corrections Found via Introspection

Both cloud streaming SDKs had API changes not reflected in the initial skeleton:

| SDK | Written as | Corrected to |
|-----|-----------|--------------|
| elevenlabs 2.41.0 | `text_to_speech.convert_as_stream()` | `text_to_speech.stream()` |
| cartesia 3.0.2 | `tts.websocket()` | `tts.websocket_connect()` |

ElevenLabs `stream()` yields `bytes` directly (no `.audio` attribute).
Cartesia `send()` signature is unchanged across the version bump.

---

## Phase 1 — Deepgram Results (Pi run, 2026-04-05)

Run from `~/deepgram-benchmark-venv` on morpheus.

| Sentence | Latency | Total | Audio | RSS |
|----------|---------|-------|-------|-----|
| Hello, how can I help you today? | 2053ms | 4696ms | 118 KB | 65 MB |
| Your CPAP therapy last night... | 3016ms | 8397ms | 238 KB | 66 MB |
| I have added a reminder... | 2771ms | 7485ms | 208 KB | 67 MB |
| **avg** | **2613ms** | | | **66 MB** |

**Latency verdict:** MARGINAL against 800ms target. This is structural — Deepgram
TTS is REST-only; full audio received before playback starts. The 800ms target
assumes streaming. REST latency is network RTT + synthesis + full transfer.
ElevenLabs Flash v2.5 (~75ms first chunk) is the right comparison.

**Memory verdict:** 66 MB RSS — well clear of the 700 MB danger zone. No OOM
risk with any cloud TTS backend.

**Audio quality (via PyAudio):** Tearing present on all Pi runs. See tearing
investigation below.

**`--save-wav` diagnostic:**
- `aplay -D plughw:0,0 deepgram_00.wav` → clean
- `aplay -D hw:0,0 -r 24000 -f S16_LE -c 1 deepgram_00.wav` → clean
- Audio content from Deepgram is clean. Tearing is in the PyAudio/PortAudio path.

---

## Open Investigation — PyAudio Tearing on bcm2835

### Symptom
All Pi runs of `compare_tts.py` produce audible tearing through the 3.5mm jack.
The same audio played via `aplay -D plughw:0,0` is clean. The same audio played
via `aplay -D hw:0,0` with explicit format flags is also clean.

### ALSA device capabilities (from `LIBASOUND_DEBUG=1`)
```
FORMAT:  U8 S16_LE          (only supported formats)
RATE:    [8000 192000]       (24kHz natively supported — no software resampling)
PERIOD_SIZE: [80 131072]
BUFFER_SIZE: [80 131072]
```
PortAudio probes S32_LE and S32_BE first (both rejected with `Invalid argument`),
then negotiates S16_LE successfully. The format errors are probing noise, not
failures. Device configuration is correct.

### What is known
- Tearing is specific to PortAudio (PyAudio wrapper); `aplay` does not tear
- Format (S16_LE) and rate (24kHz) are both natively supported — no resampling
- Content is clean (confirmed via `aplay`)
- Not synthesis starvation (Deepgram bytes fully in memory before stream opens)

### What is NOT yet known
- What period/buffer size PortAudio negotiates after format enumeration
  (the ALSA debug output was truncated before the final hw_params settlement)
- Whether PortAudio is opening `hw:` or `plughw:` internally
- Whether `frames_per_buffer` in `pa.open()` is causing underruns

### Next diagnostic steps (carry into session 2)
1. Capture the full ALSA negotiation output including the final settled params:
   `LIBASOUND_DEBUG=1 python compare_tts.py --deepgram-only 2>&1 | grep -A5 "hw_params: set_near"`
2. Try opening PyAudio with explicit `frames_per_buffer` values (4096, 8192)
   to see if larger buffers eliminate tearing
3. Write the PyAudio replay snippet (discussed; preferred over a config file):
   reads WAV header, opens stream, plays chunk-by-chunk, reports write() timing
   — use this to iterate on `frames_per_buffer` without re-hitting the API

### Root cause hypothesis (leading)
PortAudio is negotiating a smaller period/buffer than `aplay` uses by default,
causing ALSA underruns that manifest as tearing. `aplay` defaults to larger
periods. Increasing `frames_per_buffer` in `pa.open()` is the most likely fix.

---

## What's Blocked

- **Phase 2a (ElevenLabs):** needs `ELEVENLABS_API_KEY` in `.env` on Pi and
  `pip install elevenlabs` in the benchmark venv
- **Phase 1 audio quality sign-off:** depends on resolving the tearing issue
  (may be fixed by the `frames_per_buffer` change above)

---

## Commits This Session

| Hash | Description |
|------|-------------|
| fd6e8c4 | Add TTS evaluation harness and ElevenLabs/Cartesia backend skeletons |
| 485202c | Add --save-wav flag; fix ElevenLabs stream() and Cartesia websocket_connect() |
