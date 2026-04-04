# TTS Evaluation — Agent Context

## What This Is

Active evaluation sprint to replace `PiperTTS` (EU-7) with a cloud TTS backend suitable
for production on the 1 GB Pi 4.

`PiperTTS` has two independent failures that block step 8/9:
1. **OOM kill** — `en_US-lessac-medium` (~63 MB ONNX) exhausted total swap (317 MB RSS +
   385 MB swap ≈ 700 MB against 900 MB); kernel sent SIGKILL to master.py during synthesis
2. **Audio tearing** — quality below threshold independent of memory; observed before the kill

Either condition alone blocks delivery. Both must be resolved.

## End State

1. **`TTSBackend` abstract class** in `mvp-modules/forked_assistant/src/tts.py` defines
   the contract `master.py` depends on (already added — see interface section below)
2. **Cloud implementation selected** — one backend written, tested standalone, proven in
   full integrated pipeline on Pi
3. **`PiperTTS` archived** — retained in `tts.py` as reference but no longer active;
   `master.py` instantiates the new backend
4. **This folder** — effort log and per-candidate notes remain as the evaluation record

`master.py` integration point: `tts.play(agent.run(transcript))` — no change to master.py
required once the new backend satisfies the `TTSBackend` interface.

## Interface Contract

```python
class TTSBackend(ABC):
    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise and play each chunk through ALSA device 0. Blocks until done."""
        ...

    def close(self) -> None:
        """Release audio I/O resources. Call once at process exit."""
        ...
```

`play()` must:
- Accept `Iterator[str]` of sentence-boundary-aligned chunks (from `agent_session.run()`)
- Play audio through PyAudio device 0 (bcm2835, S16_LE, ALSA only)
- Block until all audio for the turn has been played
- Handle each chunk incrementally — do not buffer the full response before starting

For cloud REST backends (no native streaming), the implementation pattern is:
- Per-chunk: call API with the chunk text, receive audio bytes, write to PyAudio stream
- One PyAudio stream per turn; open before loop, close after

## Candidates

### Deepgram Aura (evaluate first — no new API key)

- **API:** `POST https://api.deepgram.com/v1/speak?model=aura-2-thalia-en`
- **Auth:** `DEEPGRAM_API_KEY` already in `.env` (same key as STT)
- **SDK path:** `dg_client.speak.v1.audio.generate(text=..., model=..., encoding=...)`
- **Encoding:** use `linear16` (raw PCM) — avoids mp3 decode on Pi; 22050 or 24000 Hz
- **Streaming status:** REST-only currently; WebSocket streaming planned but not GA
- **Latency model:** full audio received before first byte plays — but per-chunk calls
  keep the response window per-sentence (~10–20 words), not per-full-response
- **Speed control:** `?speed=0.7–1.5` query param (Aura-2 Controls, Early Access)
- **Char limit:** 2000 chars per request — sentence chunks are well within this
- **Notes:** See `deepgram_tts_notes.md` for full API reference and voice controls

### Cartesia (evaluate if Deepgram latency is marginal)

- **API:** streaming TTS via WebSocket; first audio chunk < 200ms (documented)
- **SDK:** `pip install cartesia` — not yet in Pi venv
- **Auth:** `CARTESIA_API_KEY` — new account required; add to `.env` alongside Deepgram key
- **Encoding:** PCM available — plays directly to PyAudio without decode
- **Latency advantage:** streaming means first audio arrives while synthesis continues;
  meaningful advantage over REST if per-sentence latency matters
- **Notes:** `cartesia_notes.md` (populate after evaluation)

## Evaluation Sequence

### Phase 1 — Deepgram Aura (Pi run)

`DeepgramTTS` is already written in `mvp-modules/forked_assistant/src/tts.py`.
Read it before running — note the open questions in `effort_log.md` (sample rate
to verify, SDK return type to confirm on first run).

1. Standalone test: synthesise one sentence (`"Hello, how can I help you?"`)
   - Confirm audio through 3.5mm jack
   - Measure: wall time from API call start to first audio sample out
   - Verify `_DEFAULT_SAMPLE_RATE = 24000` is correct; adjust if pitch is wrong
2. Multi-chunk test: feed a 3-sentence iterator, measure per-chunk latency
3. Memory check: `ps aux` RSS during synthesis — must stay well under 700 MB total

If per-chunk latency ≤ 1s and quality is acceptable → proceed to Phase 3.
If latency > 1s → proceed to Phase 2.

### Phase 2 — Cartesia (Pi run, only if Phase 1 marginal)

1. `pip install cartesia` in Pi venv
2. Write `CartesiaTTS(TTSBackend)` in `src/tts.py`
3. Same standalone tests as Phase 1
4. Compare first-chunk latency vs Deepgram

### Phase 3 — Integrated test (Pi run)

1. In `master.py`: replace `PiperTTS(...)` instantiation with selected backend
2. Run full cognitive turn: wake → STT → agent → TTS → back to wake_listen
3. Confirm:
   - Time from VAD_STOPPED to first audio (target < 2s with live-sentence streaming)
   - No OOM during synthesis
   - Clean Ctrl+C from wake_listen after turn
   - Multi-turn: 2–3 consecutive turns without degradation

### Phase 4 — Decision and cleanup

1. Mark selected backend as default in `mvp-modules/forked_assistant/src/tts.py`;
   comment `PiperTTS` as archived
2. Update `effort_log.md` (this folder) with latency measurements and decision rationale
3. Update `mvp-modules/forked_assistant/AGENTS.md` — What's Next to step 9 validation
4. Update `mvp-modules/forked_assistant/spec/implementation_framework.md` — step 8 complete
5. Update `mvp-modules/memory/architecture_decisions.md` — TTS section with final decision

## Evaluation Criteria

| Criterion | Target | Notes |
|-----------|--------|-------|
| Time to first audio (short sentence, full pipeline) | < 2s post-VAD_STOPPED | Accounts for STT + agent first sentence + TTS first chunk |
| Audio quality (subjective, Pi 3.5mm output) | Acceptable for CPAP/calendar/routine | Compare against Piper baseline recording if available |
| Memory footprint (master.py RSS during synthesis) | No OOM — keep headroom from 700 MB danger zone | Cloud TTS: no ONNX, expected < 100 MB delta |
| Pi 4 compatibility | No crash, no kernel OOM | Must survive 3 consecutive turns |
| Per-sentence latency (REST path) | < 800ms | Time from chunk text to first audio byte for ~15-word sentence |

## File Layout

```
tts_evaluation/
├── AGENTS.md              ← you are here
├── effort_log.md          ← running session log: findings, measurements, decisions
├── deepgram_tts_notes.md  ← Deepgram Aura API reference, voice controls, SDK patterns
└── cartesia_notes.md      ← Cartesia evaluation notes (create if Phase 2 runs)
```

## Key Files Outside This Folder

Paths are relative to the repo root (`raspberry-ai/`). This folder lives at
`mvp-modules/archive/tts_evaluation/`.

| File | Purpose |
|------|---------|
| `mvp-modules/forked_assistant/src/tts.py` | `TTSBackend` abstract class + all implementations |
| `mvp-modules/forked_assistant/src/master.py` | Integration point: `tts.play(agent.run(transcript))` |
| `mvp-modules/forked_assistant/src/agent_session.py` | `run()` yields sentence-boundary chunks |
| `mvp-modules/forked_assistant/archive/2026-04-05_open_items.md` | Item 1: TTS rearchitecture (blocking step 9) |
| `mvp-modules/memory/architecture_decisions.md` | TTS Rearchitecture section routes here |

## Hardware Context

- **Device:** Raspberry Pi 4 Model B, 1 GB RAM, quad-core ARM Cortex-A72
- **Audio output:** bcm2835 headphones (3.5mm jack), PyAudio device 0, ALSA only, S16_LE
- **Python venv:** `~/pipecat-agent/venv/` — `deepgram-sdk` already installed
- **Process:** TTS runs in master process (cores 1–3); recorder child on core 0 in idle phase
- **No process breakout needed:** cloud TTS is HTTP API calls — no ONNX, no memory pressure
- **OWW/Silero gated off** during TTS (idle phase bracket; confirmed 2026-04-04)

## No Process Breakout

The user considered a separate TTS subprocess but it is not needed. The OOM condition
was caused by Piper ONNX loading in the same process as the rest of master.py. Cloud
TTS makes HTTP calls — memory delta is negligible. Running TTS in master (cores 1–3)
is correct and simpler.
