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
3. **`PiperTTS` retired** — ported to `TTSBackend`, proven on Pi, synthesis stub left in
   place; class remains in `tts.py` as the record of why Piper was rejected (OOM + audio
   tearing); `master.py` instantiates the selected cloud backend
4. **This folder** — effort log and per-candidate notes remain as the evaluation record

`master.py` integration point: `tts.play(agent.run(transcript))` — no change to master.py
required once the new backend satisfies the `TTSBackend` interface.

## Interface Contract

```python
class TTSBackend(ABC):
    def warm(self) -> None:
        """Prime network connection and server-side model. Audio discarded.
        Call once after construction, ideally during a non-blocking window.
        Default: no-op. Override per-backend."""

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise and play each chunk through ALSA device 0. Blocks until done."""
        ...

    def close(self) -> None:
        """Release audio I/O resources. Call once at process exit."""
        ...
```

`warm()` should:
- Be called once after construction, before the first `play()` call
- Send a minimal throwaway synthesis (e.g. `"."`) to prime TCP/TLS + server-side model
- Discard all audio bytes — no `_AudioOut` needed
- Be non-fatal — log and continue if the warm-up request fails
- Be called from `master.py` during a non-blocking window (e.g. during STT or agent init)

`play()` must:
- Accept `Iterator[str]` of sentence-boundary-aligned chunks (from `agent_session.run()`)
- Play audio through ALSA device 0 (bcm2835, S16_LE) via `pyalsaaudio`
- Block until all audio for the turn has been played
- Handle each chunk incrementally — do not buffer the full response before starting

Audio output uses `pyalsaaudio` (direct `snd_pcm_writei()`, no PortAudio). PyAudio
was the confirmed cause of audio tearing on bcm2835 — PortAudio's callback thread
gets descheduled on Pi 4 ARM, causing intermittent underruns. See session 2 findings
in `session_2_wrap.md` and the `_AudioOut` abstraction in `tts.py`.

For cloud REST backends (no native streaming), the implementation pattern is:
- Per-chunk: call API with the chunk text, receive audio bytes, write to `_AudioOut`
- One `_AudioOut` instance per turn; open before loop, close after

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

### ElevenLabs (Phase 2a — evaluate first if Deepgram marginal; account already created)

- **API:** streaming via `convert_as_stream()`; `pcm_24000` output (raw S16LE, 24kHz)
- **SDK:** `pip install elevenlabs` — not yet in Pi venv
- **Auth:** `ELEVENLABS_API_KEY` — account at elevenlabs.io; key under **Profile → API Keys** (`sk_...`)
- **Model:** `eleven_flash_v2_5` — ~75ms first-chunk latency (documented)
- **Encoding:** `pcm_24000` matches Deepgram sample rate — no PyAudio reconfiguration needed
- **Latency advantage:** streaming; Flash v2.5 is the lowest-latency ElevenLabs model
- **Notes:** `elevenlabs_notes.md` (populate after evaluation)

### Cartesia (Phase 2b — evaluate only if ElevenLabs also marginal)

- **API:** streaming TTS via WebSocket; first audio chunk < 200ms (documented)
- **SDK:** `pip install cartesia` — not yet in Pi venv
- **Auth:** `CARTESIA_API_KEY` — new account required; add to `.env` alongside other keys
- **Encoding:** PCM available — plays directly to PyAudio without decode
- **Latency advantage:** streaming means first audio arrives while synthesis continues;
  meaningful advantage over REST if per-sentence latency matters
- **Notes:** `cartesia_notes.md` (populate after evaluation)

## Evaluation Sequence

### Phases 1–2 — complete (session 3, 2026-04-05)

All three backends evaluated on Pi with pyalsaaudio. Quality outstanding
across the board. No OOM risk (all under 61 MB RSS).

| Backend | Avg first-chunk | Streaming | Notes |
|---------|-----------------|-----------|-------|
| Deepgram | 2642ms | No (REST) | Same API key as STT |
| ElevenLabs | 486ms (warm) | Yes | 2.8s cold start, then excellent |
| Cartesia | 1043ms | Yes | Rushed pacing (tunable) |

Decision: keep all three as selectable `TTSBackend` modules.
See `effort_log.md` for full results and `session_3_wrap.md` for details.

### Phase — Tuning (session 4) — complete

See `tuning_plan.md` and `session_4_wrap.md` for details. Summary:

1. Tuning controls wired into `compare_tts.py` (Cartesia speed/emotion/buffer_delay,
   ElevenLabs speed/stability/similarity/optimize_latency)
2. Katie cadence fixed: `generation_config.speed = 0.85` confirmed natural
3. Warm-start measured: ElevenLabs 367ms avg, Cartesia 1090ms, Deepgram 1148ms
4. ALSA leading-truncation fixed: silence pre-fill in `_AudioOut` — confirmed clean
5. Tuning controls propagated into production `TTSBackend` classes in `src/tts.py`
6. `warm()` method added to `TTSBackend` ABC + all three implementations

### Phase — Voice audition (session 5) — complete

Cartesia voice audition: shortlist Katie / Allie / Kayla. Two rounds (short sentence
at 0.85; expressive long-form monologue at 1.0 and 0.85). Allie selected.

**Final voice selections:**

| Backend | Voice | ID | Speed default |
|---------|-------|----|---------------|
| Cartesia | Allie — Natural Conversationalist | `2747b6cf-fa34-460c-97db-267566918881` | 0.85 |
| ElevenLabs | Matilda — Knowledgeable, Professional | `XrExE9yKIg1WjnnlVkGX` | 0.85 |
| Deepgram | Helena — Caring, Natural, Positive | `aura-2-helena-en` | 1.05 |

**Platform precedence:** Cartesia (primary) → ElevenLabs (fallback) → Deepgram (tertiary).
Defaults propagated into `src/tts.py`. See `voice_tuning_results.md` for full rationale.

### Phase 3 — Integrated test (Pi run, next session)

1. In `master.py`: replace `PiperTTS(...)` with selected backend
2. Call `tts.warm()` during a non-blocking window (STT phase or agent init)
3. Full cognitive turn: wake → STT → agent → TTS → back to wake_listen
4. Confirm: time-to-first-audio < 2s, no OOM, clean Ctrl+C, multi-turn
5. Measure warm() vs no-warm() first-turn latency delta

### Phase 4 — Cleanup

1. All three backends as selectable modules in `src/tts.py`
2. Update `effort_log.md` with tuning results and final configuration
3. Update `forked_assistant/AGENTS.md` — step 9 validation
4. Update `memory/architecture_decisions.md` — TTS section

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
├── AGENTS.md                  ← you are here
├── compare_tts.py             ← standalone comparison harness (tuning sandbox)
├── replay_wav.py              ← WAV replay tool: PyAudio sweep, pyalsaaudio, aplay modes
├── effort_log.md              ← running session log: findings, measurements, decisions
├── tuning_plan.md             ← session 4 spec (complete): tuning controls, warm-start, ALSA fix
├── voice_tuning_brief.md      ← standalone brief for interactive voice tuning sessions
├── voice_tuning_results.md    ← results skeleton: per-backend trial tables
├── session_1_wrap.md          ← session 1: setup, Phase 1 results, tearing found
├── session_2_wrap.md          ← session 2: tearing root cause confirmed, pyalsaaudio fix
├── session_3_wrap.md          ← session 3: Phase 1 re-run, Phase 2 complete, decision
├── session_4_wrap.md          ← session 4: tuning controls, cadence fix, warm-start, warm() API
└── deepgram_tts_notes.md      ← Deepgram Aura API reference, voice controls, SDK patterns
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
- **Audio output:** bcm2835 headphones (3.5mm jack), ALSA `hw:0,0`, S16_LE
  - Uses `pyalsaaudio` (direct `snd_pcm_writei()`); PyAudio/PortAudio rejected (tearing)
- **Python venv:** `~/deepgram-benchmark-venv/` — scratch venv with deepgram-sdk, elevenlabs, cartesia==3.0.2
- **Process:** TTS runs in master process (cores 1–3); recorder child on core 0 in idle phase
- **No process breakout needed:** cloud TTS is HTTP API calls — no ONNX, no memory pressure
- **OWW/Silero gated off** during TTS (idle phase bracket; confirmed 2026-04-04)

## No Process Breakout

The user considered a separate TTS subprocess but it is not needed. The OOM condition
was caused by Piper ONNX loading in the same process as the rest of master.py. Cloud
TTS makes HTTP calls — memory delta is negligible. Running TTS in master (cores 1–3)
is correct and simpler.
