# Deepgram TTS — Reference Notes

Source: https://developers.deepgram.com/docs/tts-voice-controls (retrieved 2026-04-05)

---

## API Endpoint

```
POST https://api.deepgram.com/v1/speak?model=aura-2-thalia-en
```

Headers:
- `Content-Type: application/json`
- `Authorization: Token DEEPGRAM_API_KEY`

Body:
```json
{"text": "Your text here"}
```

Response: audio bytes (format determined by encoding query param).

---

## Python SDK Pattern (deepgram-sdk v6)

```python
from deepgram import DeepgramClient
from deepgram.core.request_options import RequestOptions

client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

response = client.speak.v1.audio.generate(
    text="Hello, how can I help you?",
    model="aura-2-thalia-en",
    encoding="linear16",   # raw S16_LE PCM — no decode needed
)

# response is iterable; join for full bytes or write progressively
audio_bytes = b"".join(response)
```

**Note:** Confirm whether `response` is a true streaming iterator or returns buffered
bytes. If iterable, write progressively to the PyAudio stream for lower first-audio
latency. Check on first Pi run.

---

## Encoding Options

| Encoding | Format | Notes |
|----------|--------|-------|
| `linear16` | Raw PCM S16_LE | No decode needed; write directly to PyAudio paInt16 |
| `mp3` | MPEG Layer 3 | Smaller payload; requires decode — adds dependency on Pi |
| `mulaw` | μ-law | Telephony; not useful here |
| `alaw` | A-law | Telephony; not useful here |

**Recommendation:** Use `linear16`. Avoids adding an mp3 decode dependency on Pi,
and no memory/CPU overhead from decoding.

**Sample rate:** Aura-2 returns 24000 Hz PCM for `linear16`. Open PyAudio stream at
24000 Hz to avoid pitch shift. Verify from response or SDK docs on first run.

---

## Speed Control (Aura-2 Controls — Early Access)

Query param: `?speed=0.9`

| Value | Effect |
|-------|--------|
| 0.7 | 30% slower |
| 0.9 | 10% slower — recommended for clear voice assistant responses |
| 1.0 | Default |
| 1.2 | 20% faster |

**Status:** Early Access as of 2026-04. REST API only; WebSocket coming.

**Pi recommendation:** Test at `speed=0.9` for voice assistant use. Slightly slower
delivery is more comfortable for the CPAP/calendar/routine use cases at 1m distance.

**SDK integration:**

```python
from deepgram.core.request_options import RequestOptions

opts = RequestOptions(additional_query_parameters={"speed": "0.9"})
response = client.speak.v1.audio.generate(
    text=chunk,
    model="aura-2-thalia-en",
    encoding="linear16",
    request_options=opts,
)
```

---

## Pronunciation Control (Aura-2 Controls — Early Access)

Inline IPA notation within text. Useful for names, medical terms, brand names.

```python
# Raw string; escaped braces are literal in the text payload
text = r'Take \{"word": "Azathioprine", "pronounce": "æzəˈθaɪəpriːn"\} twice daily.'
```

**Limits:**
- Max 500 pronunciations per request
- Max IPA string: 128 chars
- Max input text: 2000 chars per request — sentence chunks are well within this

**Billing:** IPA notation is not billed; underlying word is billed.

Not immediately needed for step 9 use cases, but useful for future persona tuning
(e.g., pronouncing "morpheus" correctly, or medical terminology for CPAP use).

---

## Char Limit

2000 characters per request. Sentence chunks from `agent_session._flush_sentences()`
are bounded by sentence boundaries — typical range 20–200 chars. No chunking needed
beyond the existing sentence-level streaming.

---

## Available English Voices (Aura-2)

A representative sample — full list at https://developers.deepgram.com/docs/tts-models

| Voice ID | Gender | Character |
|----------|--------|-----------|
| `aura-2-thalia-en` | F | Warm, conversational |
| `aura-2-orion-en` | M | Clear, professional |
| `aura-2-luna-en` | F | Soft, calm |
| `aura-2-asteria-en` | F | Expressive |

Recommended for voice assistant at 1m distance: `aura-2-thalia-en` (warm, natural
cadence) or `aura-2-orion-en` (clear enunciation). Evaluate both on first Pi run.

---

## Response Headers (useful for diagnostics)

```
dg-request-id: req_xyz789
dg-model-name: aura-2-thalia-en
dg-char-count: 47
dg-pronunciations-applied: 2
dg-speed-used: 0.8
```

`dg-char-count` confirms billing. Log on first run to verify expected charge.

---

## Streaming Status

As of 2026-04-05: REST API only for Aura-2 Controls (speed + pronunciation).
WebSocket streaming is planned but not GA. Standard Aura (v1) models may have
WebSocket support — check SDK if REST latency proves marginal.

**Latency implication:** REST means full audio response received before play starts.
For a ~15-word sentence (≈ 80–120 chars), round-trip latency to first audio byte will
be: network RTT + synthesis time + full audio transfer. Expected: 200–600ms depending
on network and sentence length. Measure on first Pi run.

---

## Error Codes

| Code | Meaning |
|------|---------|
| `speed_out_of_range` | Speed param outside 0.7–1.5 |
| `pronunciation_invalid` | Invalid IPA notation |
