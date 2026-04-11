# A/B Comparison: 4-mic vs 2-mic Pipeline Frame Dumps

**Date:** 2026-04-11
**Predecessor:** `pipeline_frame_dump_findings.md` (2-mic analysis)

---

## Result

**VAD fires on the 4-mic HAT. VAD does not fire on the 2-mic HAT.**
Same pipeline code, same Pi, same session flow. The bug is specific to
2-mic audio interacting with Pipecat's `SileroVADAnalyzer` — not the
pipeline wiring.

---

## Test conditions

| | 2-mic (WM8960) | 4-mic (AC108) |
|---|---|---|
| Dump file | `~/pipeline_dump_20260411_104958.pcm` | `~/pipeline_dump_20260411_110205.pcm` |
| Size | 435 840 bytes | 853 120 bytes |
| Duration | 13.6 s (681 frames) | 26.7 s (1 333 frames) |
| VAD fired? | **No** | **Yes** |
| USB charger proximity | Adjacent to Pi | Moved away |
| HAT overlay | `seeed-2mic-voicecard` | `seeed-4mic-voicecard` |

---

## Signal comparison

### Amplitude

| Metric | 2-mic | 4-mic |
|---|---|---|
| Global std dev | High (see RMS) | 596 |
| Median frame RMS | High | **34** |
| Frames RMS > 2 000 | **42 %** | **2.6 %** |
| Frames RMS > 1 000 | — | 3.5 % |
| Frames RMS < 500 | **9 %** | **95.4 %** |
| Frames RMS < 100 | — | 93.2 % |
| Clipped samples | 131 (0.06 %) | **0** |
| Min / Max | −32 767 / +32 767 | −20 457 / +12 788 |

The 2-mic signal is **massively hotter** — the WM8960's default capture
gain drives most frames into the range Silero would consider "shouting",
while the 4-mic AC108 delivers a quiet floor (~34 RMS) with brief speech
bursts up to ~5 300 RMS. The 4-mic's dynamic range is clean and natural;
the 2-mic is compressed and clipping.

### DC offset

| Metric | 2-mic | 4-mic |
|---|---|---|
| Per-second DC range | −860 to +619 | Stable −8 throughout |
| Startup transient | −24 500 (frame 0) | +1 643 (frame 0) |

The 2-mic DC wanders by ~1 500 units over 13 s. The 4-mic is rock-steady.
Both codecs show a startup transient from coupling-capacitor charge-up,
but the 2-mic's is 15× larger.

### Spectral energy

| Band | 2-mic | 4-mic |
|---|---|---|
| 0–100 Hz | — | 1.2 % |
| 100–300 Hz | 20 % | 1.5 % |
| 300–1 000 Hz | 52 % (300–3 kHz) | 58.3 % |
| 1 000–3 000 Hz | (included above) | 32.1 % |
| 3 000–8 000 Hz | — | 7.0 % |
| Dominant peak | ~435 Hz, ~576 Hz | ~573 Hz |

Both HATs capture speech-band energy. The 4-mic has tighter spectral
concentration in the 300–3 000 Hz voice band (~90 %). The 2-mic's broader
low-frequency content (20 % in 100–300 Hz) likely includes DC-drift
artefacts and power-supply hum.

---

## Key observation: harness vs pipeline

The standalone `vad_harness.py` fires VAD correctly on the **same 2-mic
HAT**, using the same ALSA device and Silero ONNX model. This means:

- **Raw Silero has no problem with the hot 2-mic signal.**
- The bug is in how **Pipecat's `SileroVADAnalyzer`** (or `VADController`)
  processes the signal — not in Silero itself.

The harness calls `silero_vad` directly: feed 512-sample chunks, read
the probability. Pipecat wraps Silero in `SileroVADAnalyzer` which adds
windowing, `start_secs` / `stop_secs` confidence accumulation, and
internal state management. Something in that wrapper layer behaves
differently with the 2-mic's signal profile.

---

## Refined hypotheses

Previous hypotheses 1 (phase gating) and 3 (ONNX contention) are
**eliminated** — VAD fires on 4-mic through the same gating and ONNX
configuration.

Previous hypothesis 4 (ARM-specific Silero) is **eliminated** — Silero
works fine on ARM via the harness.

**Surviving / refined hypotheses:**

### H1: Pipecat VADAnalyzer amplitude handling

`SileroVADAnalyzer` may normalize or window audio before feeding it to
Silero. If it divides by `max_int16` (32 767) to get float32 in [−1, 1],
the 2-mic's near-clipping signal would produce values near ±1.0
throughout, flattening the speech/silence contrast that Silero needs to
detect transitions. The 4-mic's quiet floor (~34 RMS, <0.1 % of full
scale) gives Silero a clear silence→speech edge.

### H2: Pipecat VADAnalyzer confidence accumulation

`start_secs` (default 0.2 s) requires sustained above-threshold
confidence. If the 2-mic's DC drift or clipping causes Silero's per-chunk
probability to oscillate rather than stay consistently above threshold,
the accumulator resets repeatedly and never crosses `start_secs`. The
4-mic's clean transitions produce a monotonic confidence ramp.

### H3: DC offset confuses Silero's internal state

Silero VAD uses a stateful LSTM/GRU. A wandering DC baseline (mean
shifting by ~1 500 over seconds) might prevent the internal state from
settling, keeping confidence in an ambiguous zone. The 4-mic's stable
DC = −8 gives the model a consistent reference.

---

## Next steps

### 1. Frame dump through the harness (2-mic) — READY

`vad_harness.py` now imports and composes the same `FrameDumpProcessor`
from `assistant/frame_dump.py` that the recorder pipeline uses.
Activated by the same env var; output files use prefix `harness_dump_`
(vs the pipeline's `pipeline_dump_`) for easy A/B comparison.

```bash
PIPELINE_FRAME_DUMP=1 python mvp-modules/vad-only/vad_harness.py
```

If the harness dump is byte-identical to the pipeline dump (same HAT,
same session), the difference is purely in how Pipecat's
`SileroVADAnalyzer` wrapper processes the signal — not the audio
content reaching it.

### 2. Inspect Pipecat `SileroVADAnalyzer` source

Read the actual Pipecat source for `SileroVADAnalyzer.process_frame()`
and `VADController` to understand:
- Does it normalize amplitude before Silero inference?
- How does `start_secs` accumulation work — reset on every below-threshold
  chunk, or decaying window?
- Does it apply any DC removal or windowing?

### 3. Log Silero confidence per-frame

Instrument `GatedVADProcessor` (or a new diagnostic processor) to log
Silero's raw probability output for every frame during `capture` phase.
Compare 2-mic vs 4-mic confidence traces. This directly reveals whether
Silero is returning low probabilities (signal problem) or high
probabilities that the accumulator fails to integrate (wrapper problem).

### 4. Reduce WM8960 capture gain

As a potential fix (independent of root-cause understanding):
```bash
amixer -c 1 sset 'Capture' 80%   # or lower
```
If bringing the 2-mic amplitude down to 4-mic levels makes VAD fire,
that confirms H1 (amplitude saturation in the wrapper).

---

## Procedure: reinstall 2-mic HAT

Before the next debugging session:

1. Power down the Pi.
2. Swap the 4-mic ReSpeaker HAT for the 2-mic WM8960 HAT.
3. Boot, verify `arecord -l` shows the WM8960 card.
4. Run the next diagnostic (harness dump or confidence logging).

---

## Reference

| File | Role |
|---|---|
| `~/pipeline_dump_20260411_104958.pcm` | 2-mic dump (WM8960, 13.6 s) |
| `~/pipeline_dump_20260411_104958.meta` | 2-mic sidecar |
| `~/pipeline_dump_20260411_110205.pcm` | 4-mic dump (AC108, 26.7 s) |
| `~/pipeline_dump_20260411_110205.meta` | 4-mic sidecar |
| `assistant/frame_dump.py` | `FrameDumpProcessor` — shared by pipeline and harness (`prefix` param for filename) |
| `assistant/recorder_process.py` | Pipeline composition |
| `mvp-modules/vad-only/vad_harness.py` | Standalone harness (VAD fires on 2-mic); composes `FrameDumpProcessor` for A/B dumps |
| `mvp-modules/vad-only/pipeline_frame_dump_findings.md` | Previous 2-mic-only analysis |
| `mvp-modules/vad-only/pipeline_frame_dump_brief.md` | Original frame dump brief |
