# Signal levels (WM8960 / 2-mic appliance)

This folder holds **tuning context**, **measurement runs**, and **CLI helpers** for capture level on the Seeed 2-mic WM8960 HAT. The 4-mic AC108 path is different hardware (no code here applies to it).

## Where behaviour lives (code)

**`assistant/alsa_capture_mixer.py`** — Recorder child applies WM8960 capture gain via `amixer` **before** PyAudio opens the stream. **Appliance defaults:** input boost **2**, PGA **45** (both channels), when the Seeed WM8960 card is detected in `/proc/asound/cards`. No `.env` required on ship builds.

- Disable entirely: `RECORDER_ALSA_CAPTURE_MIXER=off` (or `0` / `false`).
- Force card or override levels: see the module docstring (also lists `legacy_hot` preset = old boost 3 / PGA 39).

Hook site: `assistant/recorder_process.py` → `apply_recorder_alsa_capture_mixers()`.

## Files in this folder

| Path | Role |
|------|------|
| `context.md` | Design context, gain chain, constraints, **post-tune** reference numbers |
| `capture_stats.py` | Live `arecord` + same stats as `vad-only/analyze_dump.py` (fast iteration without pipeline) |
| `runs/` | Timestamped measurement JSON / text; raw ``*.pcm`` here is **gitignored** (use ``capture_stats.py --save`` locally) |
| `session_*.md` | Dated work logs (e.g. `session_2026-04-11_wm8960_levels.md`) |

## Related tools elsewhere

| Path | Role |
|------|------|
| `mvp-modules/vad-only/analyze_dump.py` | Analyse 16 kHz mono int16 pipeline dumps |
| `assistant/frame_dump.py` | `PIPELINE_FRAME_DUMP=1` — PCM from inside Pipecat |

## Quick commands

```bash
source ~/venv/bin/activate
python mvp-modules/signal_levels/capture_stats.py -D plughw:CARD=seeed2micvoicec,DEV=0 --seconds 15
python mvp-modules/vad-only/analyze_dump.py ~/pipeline_dump_before.pcm ~/pipeline_dump_after.pcm
```
