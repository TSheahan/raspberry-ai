# Signal Levels — Context for Optimisation

**Work folder:** `mvp-modules/signal_levels/`  
**Date:** 2026-04-11 (updated through appliance ship)  
**Predecessor:** `mvp-modules/vad-only/` (VAD troubleshoot, resolved)

**Status:** WM8960 capture gain is **tuned and enforced at runtime** by `assistant/alsa_capture_mixer.py` (appliance defaults: input boost **2**, PGA **45**). See `README.md` in this folder and `session_2026-04-11_wm8960_levels.md` for the operator narrative.

---

## Situation (historical)

The voice assistant pipeline worked end-to-end on the 2-mic WM8960 HAT, but capture ran **very hot** — median frame RMS ~1 500 on a ±32 768 scale, with ~35 % of frames above 2 000 RMS and occasional clipping. That left little headroom for louder conditions or EMI. **Gain was reduced and automated** (see Resolution below).

---

## WM8960 gain chain — deployed vs previous (card `seeed2micvoicec`)

Values are L/R symmetric. dB figures for PGA follow the driver’s `dBscale-min` + step × value; boost steps follow the WM8960 LINPUT1 scale (see session log).

| Stage | ALSA control | **Deployed (appliance)** | Previous (hot lab profile) |
|------|----------------|--------------------------|----------------------------|
| PGA | `Capture Volume` | **45** / 63 | 39 / 63 |
| Input boost | `Left Input Boost Mixer LINPUT1 Volume` (and R) | **2** / 3 (≈ +20 dB stage) | 3 / 3 (≈ +29 dB) |
| Boost switch | `Left Input Mixer Boost Switch` | on | on |
| ADC digital | `ADC PCM Capture Volume` | 195 / 255 (0 dB) | 195 / 255 |
| ADC HP filter | `ADC High Pass Filter Switch` | **off** | **off** |
| ALC | `ALC Function` | **off** | **off** |

Other HATs (e.g. AC108) are unchanged — the recorder mixer hook **no-ops** unless the WM8960 Seeed card is detected or a card is forced via environment (see `alsa_capture_mixer.py`).

---

## Reference signal profiles

From `mvp-modules/vad-only/analyze_dump.py` on 16 kHz mono int16 pipeline dumps (30 s):

| Metric | 2-mic hot (`113946`) | 2-mic tuned (`124307`) | 4-mic AC108 (`110205`, ref) |
|--------|----------------------|-------------------------|-----------------------------|
| Median frame RMS | 1 542 | 439 | 34 |
| Frames RMS > 2 000 | 34.9 % | 6.1 % | 2.6 % |
| Frames RMS < 500 | 8.9 % | 55.7 % | 95.4 % |
| Clipped samples | 2 | 2 | 0 |
| DC offset range (per sec) | −458 to +770 | −196 to +225 | −8 to +96 |
| Startup frame 0 mean | −24 160 | +3 360 | +1 643 |

The tuned 2-mic dump used the **same wake/speech-style scenario** as the hot baseline; the large level drop reflects the new gain path. Spectrum and DC also differ; see session log for the full `analyze_dump` comparison. The AC108 column remains the “quiet floor / huge VAD margin” reference — different physics, not a target to match numerically on WM8960.

---

## Pipecat VAD parameters (current)

```python
SileroVADAnalyzer(
    params=VADParams(stop_secs=1.8, start_secs=0.2, min_volume=0.0),
)
```

`min_volume=0.0` disables the volume gate — set during VAD troubleshoot. Default was 0.6. **Candidate for revisiting** once levels have been lived with in production.

---

## Available levers

### Hardware (ALSA mixer)

1. **Reduce input boost** — largest coarse step (was central to Apr 2026 tuning).
2. **Reduce or raise PGA** — fine steps (0.75 dB per step on `Capture Volume`).
3. **Enable ADC HP filter** — removes DC wander at source.
4. **Enable ALC** — hardware AGC; more behaviour to validate.

### Software (pipeline code)

5. **DC removal filter** — IIR high-pass before VAD.
6. **Software AGC / normalisation** — heavier; last resort on this Pi.
7. **Re-enable `min_volume` gate** — after level stability is proven.
8. **`InputQualityProcessor`** — `assistant/input_quality.py`, `INPUT_QUALITY_CHECK=1`; possible evolution toward level telemetry.

---

## Tools

| File | Purpose |
|------|---------|
| `mvp-modules/signal_levels/capture_stats.py` | Live ALSA capture + stats (same metrics as `analyze_dump.py`) |
| `mvp-modules/vad-only/analyze_dump.py` | PCM dump analysis — per-dump report + comparison table |
| `assistant/alsa_capture_mixer.py` | **Runtime** WM8960 `amixer` apply in recorder child (appliance defaults + env) |
| `assistant/frame_dump.py` | Pipeline PCM (`PIPELINE_FRAME_DUMP=1`) |
| `assistant/input_quality.py` | Env-gated inline quality window |
| `mvp-modules/vad-only/vad_harness.py` | Standalone VAD probe |

---

## Dump files (examples)

| File | Description |
|------|-------------|
| `~/pipeline_dump_20260411_113946.pcm` | 2-mic pipeline, **pre-tune** hot baseline (30 s) |
| `~/pipeline_dump_20260411_124307.pcm` | 2-mic pipeline, **post-tune** paired comparison (30 s) |
| `~/pipeline_dump_20260411_110205.pcm` | 4-mic AC108 pipeline — spectral / level reference only |

Full dump registry: `mvp-modules/vad-only/dumps.md`.

---

## Resolution — runtime integration

- **`assistant/alsa_capture_mixer.py`** applies WM8960 capture settings **before** `LocalAudioTransport` opens audio, when the Seeed WM8960 card is present (auto-detect from `/proc/asound/cards`) unless `RECORDER_ALSA_CAPTURE_MIXER=off`.
- **Defaults** match the Apr 2026 speech baseline: **boost 2**, **PGA 45** (see `runs/speech_20260411_122539/run_summary.txt`).
- **Operator docs:** `README.md` (this folder), `session_2026-04-11_wm8960_levels.md` (session narrative), and the module docstring in `alsa_capture_mixer.py`.

---

## Key constraints

- **Raspberry Pi 4, 1 GB RAM** — per-frame work stays light (no heavy DSP).
- **Silero VAD** — benefits from believable silence→speech contrast; always-loud or always-quiet hurts.
- **STT (Deepgram)** — validate gain changes against real transcripts in the field.
- **Two HATs in play** — WM8960 logic must not run on AC108; the current code is **detection-gated** and override-capable via environment.
