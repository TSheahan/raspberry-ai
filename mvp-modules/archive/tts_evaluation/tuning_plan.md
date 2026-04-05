# TTS Tuning Plan — Session 4 (complete)

## Status: DONE (2026-04-05)

All items complete. Results in `session_4_wrap.md`. Key outcomes:
- Cartesia speed 0.85 confirmed natural for Katie
- ALSA silence pre-fill fixes leading truncation
- `warm()` added to `TTSBackend` ABC + all implementations
- ElevenLabs warm-start avg 367ms; no cold-start recurrence at 0.5s intervals
- Next: wire `tts.warm()` call into `master.py` (Phase 3)

## Goal

Expose tuning controls for all three backends in `compare_tts.py`.
Run warm-start and cadence experiments. Propagate proven settings into
production `TTSBackend` classes in `src/tts.py`.

Use short sentences only — long ones carry no benefit for latency testing.

## Venv

Scratch venv with all three providers installed:

    source ~/deepgram-benchmark-venv/bin/activate

## 1. Wire Cartesia tuning controls

Add CLI args to `compare_tts.py`, pass through to `connection.send()`:

- `--cartesia-speed FLOAT` → `generation_config.speed` (0.6–1.5, default 1.0)
- `--cartesia-emotion NAME` → `generation_config.emotion` (Happy, Calm, Angry, etc.)
- `--cartesia-buffer-delay MS` → `max_buffer_delay_ms` (0–5000, default 3000)

The `generation_config` dict is added to the send payload only when
non-default values are present. The `max_buffer_delay_ms` is a top-level
field in the send dict.

### Katie cadence fix

Run with decreasing speed values to find natural pacing:

    python compare_tts.py --cartesia-only \
      --cartesia-voice-id f786b574-daa5-4673-aa0c-cbe3e8534c02 \
      --cartesia-speed 0.9 \
      --sentence "Hello, how can I help you today?"

Test 0.9, 0.85, 0.8. Pick the value that sounds natural.

## 2. Wire ElevenLabs tuning controls

Add CLI args, pass through to `client.text_to_speech.stream()`:

- `--el-speed FLOAT` → `voice_settings.speed` (0.25–4, recommended 0.5–2)
- `--el-stability FLOAT` → `voice_settings.stability` (0–1)
- `--el-similarity FLOAT` → `voice_settings.similarity_boost` (0–1)
- `--el-optimize-latency INT` → `optimize_streaming_latency` (0–4)

`voice_settings` is passed as a dict to `stream()`. Only include when
any value is non-default. `optimize_streaming_latency` is a separate kwarg.

## 3. Warm-start investigation

Use a single short sentence. Run each backend 3–5 times back-to-back
with minimal pause. Compare first-call vs subsequent-call latency.

    --sentence "Hello." --pause 0.5

### ElevenLabs

The `ElevenLabs()` client uses `httpx` internally. Connection reuse via
HTTP keep-alive is implicit. Question: does the 2.8s cold start recur
after idle? Test with increasing pauses (0.5s, 5s, 30s) between calls.

### Cartesia

`websocket_connect()` opens a fresh WebSocket per call. Potential
improvement: open the connection once, send multiple requests with
separate `context_id` values. The v3 SDK `connection.context()` pattern
supports this natively. Test single-connection multi-sentence.

### Deepgram

REST with `DeepgramClient` reuse (already done). HTTP keep-alive depends
on SDK internals. Limited improvement without WebSocket streaming. Test
back-to-back to confirm no cold-start penalty.

## 4. ALSA leading-truncation fix

`_AudioOut` drops ~100ms on a freshly opened ALSA device. Fix: write one
period of silence (zero bytes) on the first `write()` call.

Affects both `compare_tts.py` and `src/tts.py` — same `_AudioOut` class.

## 5. Propagate to production

After tuning, update `src/tts.py`:

- `CartesiaTTS.__init__` — accept `speed`, `emotion`; pass as
  `generation_config` in `_synthesise_to_output`
- `ElevenLabsTTS.__init__` — accept `voice_settings` dict and
  `optimize_streaming_latency`; pass in `_synthesise_to_output`
- `DeepgramTTS` — no changes needed (speed already wired)

## Control surface reference

### Cartesia `connection.send()` dict

```python
{
    "context_id": "tts",
    "model_id": "sonic-3",
    "transcript": text,
    "voice": {"mode": "id", "id": voice_id},
    "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 22050},
    "generation_config": {         # sonic-3 only
        "speed": 0.85,             # 0.6–1.5, default 1.0
        "emotion": "Calm",         # enum, optional
        "volume": 1.0,             # 0.5–1.5, default 1.0
    },
    "max_buffer_delay_ms": 1000,   # 0–5000, default 3000
    "language": "en",              # optional
}
```

### ElevenLabs `text_to_speech.stream()` kwargs

```python
client.text_to_speech.stream(
    voice_id=voice_id,
    text=text,
    model_id="eleven_flash_v2_5",
    output_format="pcm_24000",
    optimize_streaming_latency=2,      # 0–4
    voice_settings={
        "stability": 0.5,             # 0–1
        "similarity_boost": 0.75,     # 0–1
        "style": 0.0,                 # 0–1 (adds latency)
        "speed": 1.0,                 # 0.25–4
        "use_speaker_boost": True,    # bool (adds latency)
    },
)
```

### Deepgram `speak.v1.audio.generate()` params

```python
client.speak.v1.audio.generate(
    text=text,
    model="aura-2-thalia-en",
    encoding="linear16",
    request_options=RequestOptions(
        additional_query_parameters={"speed": "0.9"}  # 0.7–1.5
    ),
)
```

No additional quality knobs. Speed is the only tunable.
