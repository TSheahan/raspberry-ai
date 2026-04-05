# Voice Tuning Brief

## Goal

Explore the tuning control surface for each TTS backend, one at a time,
in a tight listen-and-adjust loop with the user. The user listens on the
Pi's 3.5mm jack and gives subjective feedback after each run. You propose
settings, run the command, hear the verdict, and iterate.

Record every trial and the final chosen settings in `voice_tuning_results.md`
(same folder as this file).

## Tool

The only tool you need is `compare_tts.py` in this folder. Activate the
venv first:

    source ~/deepgram-benchmark-venv/bin/activate

Then run from the repo root (`~/raspberry-ai/`):

    python mvp-modules/archive/tts_evaluation/compare_tts.py [OPTIONS]

### Full CLI reference

```
usage: compare_tts.py [-h] [--deepgram-only | --elevenlabs-only |
                      --cartesia-only] [--sentence TEXT] [--save-wav [DIR]]
                      [--pause SECS] [--dg-model NAME] [--dg-speed FLOAT]
                      [--el-voice-id ID] [--el-model NAME]
                      [--cartesia-model NAME] [--cartesia-voice-id UUID]
                      [--cartesia-rate HZ] [--cartesia-speed FLOAT]
                      [--cartesia-emotion NAME] [--cartesia-buffer-delay MS]
                      [--el-speed FLOAT] [--el-stability FLOAT]
                      [--el-similarity FLOAT] [--el-optimize-latency INT]

options:
  -h, --help            show this help message and exit
  --deepgram-only       Run Deepgram only
  --elevenlabs-only     Run ElevenLabs only
  --cartesia-only       Run Cartesia only
  --sentence TEXT       Test sentence (repeatable; replaces defaults)
  --save-wav [DIR]      Save each sentence as a WAV file for aplay diagnostics
                        (default dir: current directory)
  --pause SECS          Pause between backends within a sentence (default: 2.0)
  --dg-model NAME       Deepgram voice ID (default: aura-2-thalia-en)
  --dg-speed FLOAT      Deepgram speed 0.7–1.5 (default: 0.9)
  --el-voice-id ID      ElevenLabs voice ID (default: Rachel — 21m00Tcm4TlvDq8ikWAM)
  --el-model NAME       ElevenLabs model ID (default: eleven_flash_v2_5)
  --cartesia-model NAME Cartesia model ID (default: sonic-3)
  --cartesia-voice-id UUID
                        Cartesia voice UUID — find one at
                        https://play.cartesia.ai/voices
  --cartesia-rate HZ    Cartesia PCM sample rate (default: 22050)
  --cartesia-speed FLOAT
                        Cartesia speed 0.6–1.5 (default: 1.0; try 0.85 for Katie)
  --cartesia-emotion NAME
                        Cartesia emotion (Happy, Calm, Angry, etc.)
  --cartesia-buffer-delay MS
                        Cartesia max_buffer_delay_ms 0–5000 (default: 3000)
  --el-speed FLOAT      ElevenLabs speed 0.25–4 (default: 1.0)
  --el-stability FLOAT  ElevenLabs stability 0–1
  --el-similarity FLOAT ElevenLabs similarity_boost 0–1
  --el-optimize-latency INT
                        ElevenLabs optimize_streaming_latency 0–4
```

## Control surface per backend

### Deepgram

Only one knob. REST-only (no streaming), so latency is structural.

| Parameter | CLI flag | Range | Default |
|-----------|----------|-------|---------|
| speed | `--dg-speed` | 0.7–1.5 | 0.9 |
| voice model | `--dg-model` | Aura-2 voice IDs | aura-2-thalia-en |

Voice model is the main exploration axis — different Aura-2 voices
have different tonal qualities. Speed is the only synthesis knob.

### ElevenLabs

Rich control surface. Streaming backend (~350ms warm first-chunk).

| Parameter | CLI flag | Range | Default | Notes |
|-----------|----------|-------|---------|-------|
| speed | `--el-speed` | 0.7–1.2 | 1.0 | API rejects values outside this range (confirmed 2026-04-05) |
| stability | `--el-stability` | 0–1 | server default | Higher = more consistent, lower = more expressive |
| similarity_boost | `--el-similarity` | 0–1 | server default | Higher = closer to original voice |
| style | — | 0–1 | 0.0 | Not wired to CLI; adds latency |
| use_speaker_boost | — | bool | True | Not wired to CLI; adds latency |
| optimize_streaming_latency | `--el-optimize-latency` | 0–4 | server default | Higher = faster but lower quality |
| voice ID | `--el-voice-id` | any ElevenLabs voice ID | 21m00Tcm4TlvDq8ikWAM (Rachel) | |
| model | `--el-model` | model IDs | eleven_flash_v2_5 | Flash = lowest latency |

API-level detail (`text_to_speech.stream()` kwargs):

```python
client.text_to_speech.stream(
    voice_id=voice_id,
    text=text,
    model_id="eleven_flash_v2_5",
    output_format="pcm_24000",
    optimize_streaming_latency=2,
    voice_settings={
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "speed": 1.0,
        "use_speaker_boost": True,
    },
)
```

### Cartesia

Streaming backend (~1s first-chunk). Emotion control is unique to Cartesia.

| Parameter | CLI flag | Range | Default | Notes |
|-----------|----------|-------|---------|-------|
| speed | `--cartesia-speed` | 0.6–1.5 | 1.0 | 0.85 proven for Katie |
| emotion | `--cartesia-emotion` | Happy, Calm, Angry, etc. | neutral | Enum values |
| max_buffer_delay_ms | `--cartesia-buffer-delay` | 0–5000 | 3000 | Lower = faster first chunk, choppier |
| volume | — | 0.5–1.5 | 1.0 | Not wired to CLI |
| voice ID | `--cartesia-voice-id` | any Cartesia voice UUID | (required) | |
| model | `--cartesia-model` | model IDs | sonic-3 | |

API-level detail (`connection.send()` dict):

```python
{
    "context_id": "tts",
    "model_id": "sonic-3",
    "transcript": text,
    "voice": {"mode": "id", "id": voice_id},
    "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 22050},
    "generation_config": {
        "speed": 0.85,
        "emotion": "Calm",
        "volume": 1.0,
    },
    "max_buffer_delay_ms": 1000,
    "language": "en",
}
```

## Voice IDs

| Backend | Voice | ID |
|---------|-------|----|
| Cartesia | Katie — Friendly Fixer (runner-up) | `f786b574-daa5-4673-aa0c-cbe3e8534c02` |
| Cartesia | Allie — Natural Conversationalist (**selected**) | `2747b6cf-fa34-460c-97db-267566918881` |
| Cartesia | Kayla — Easygoing Pal | `1ac31ebd-9113-405b-9d80-4a4bbbeea91c` |
| Cartesia | Callie — Encourager (reserve) | `00a77add-48d5-4ef6-8157-71e5437b282d` |
| ElevenLabs | Rachel | `21m00Tcm4TlvDq8ikWAM` |
| Deepgram | Thalia | `aura-2-thalia-en` |

## Baseline settings (already proven)

| Backend | Setting | Value | Source |
|---------|---------|-------|--------|
| Cartesia | speed | 0.85 | Session 4 — fixes Katie's rushed cadence |
| Deepgram | speed | 0.9 | Session 1 — slightly slower for clarity |

## Workflow

1. User tells you which backend to explore (one at a time).
2. You propose a setting combination and a short test sentence.
3. Run the command with `--<backend>-only` and the proposed settings.
4. User listens and gives feedback (natural / rushed / too slow / harsh / etc.).
5. You adjust and re-run. Iterate until the user is satisfied or wants to move on.
6. Record every trial in `voice_tuning_results.md`.
7. When a backend is done, fill in the "Chosen settings" block in the results doc.

Use short sentences only — long ones add no signal for tuning.
Good test sentences:
- "Hello, how can I help you today?"
- "Your appointment is at three PM tomorrow."
- "I've set a reminder for you."

## Hardware context

Raspberry Pi 4, 1 GB RAM. Audio output: 3.5mm jack, ALSA `hw:0,0`.
Audio library: `pyalsaaudio`. The user is listening on the Pi's speaker/headphones.
