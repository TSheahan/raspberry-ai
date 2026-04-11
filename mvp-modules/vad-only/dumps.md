# Pipeline Frame Dumps — Registry

**Work folder:** `mvp-modules/vad-only/`

---

## Dump 1 — 2-mic WM8960 (VAD does not fire)

| Field | Value |
|---|---|
| Files | `~/pipeline_dump_20260411_104958.pcm`, `.meta` |
| Date | 2026-04-11 10:49 |
| HAT | 2-mic WM8960 (`seeed-2mic-voicecard`) |
| Size | 435 840 bytes |
| Duration | 13.6 s (681 frames × 20 ms) |
| Format | 16 kHz, mono, int16 LE |

**Narrative.** First pipeline-level frame dump, captured to diagnose why
Silero VAD fires in the standalone harness but not inside the Pipecat
recorder pipeline on the 2-mic HAT. USB charger cord was adjacent to the
Pi/HAT — later moved away for dump 3. The dump proved the audio path is
healthy — real speech with good spectral content reaches the VAD processor.
The signal is massively hot (42 % of frames RMS > 2 000, 130 clipped
samples at ±32 767) with a large startup DC transient (−24 494 on frame 0)
and a wandering DC offset (−860 to +619 over 13 s). Despite the loud, valid
audio, Pipecat's `SileroVADAnalyzer` never fires `speech_started`. This
ruled out the audio path and pointed the investigation toward how the
Pipecat VAD wrapper handles a near-clipping, DC-drifting signal.

**⚠ Confound:** the USB charger proximity may have contributed to the
extreme amplitude, clipping, and DC wander. Dump 3 (same HAT, cord moved,
post-reboot) shows median RMS halved (780 vs 1 702), zero clipping, and
tighter DC wander (±367 vs ±860). The startup transient is near-identical,
confirming that's purely hardware (coupling-capacitor charge-up). The
steady-state difference suggests dump 1's signal profile was abnormally hot,
possibly from EMI coupling through the USB cable.

**Analysis:** `pipeline_frame_dump_findings.md`

---

## Dump 2 — 4-mic AC108 (VAD fires)

| Field | Value |
|---|---|
| Files | `~/pipeline_dump_20260411_110205.pcm`, `.meta` |
| Date | 2026-04-11 11:02 |
| HAT | 4-mic ReSpeaker AC108 (`seeed-4mic-voicecard`) |
| Size | 853 120 bytes |
| Duration | 26.7 s (1 333 frames × 20 ms) |
| Format | 16 kHz, mono, int16 LE |

**Narrative.** A/B control dump — same Pi, same pipeline code, 4-mic HAT
physically swapped in. VAD fires correctly on this HAT. The signal is clean:
median frame RMS of 34 (vs the 2-mic's thousands), 95 % of frames below
500 RMS, zero clipped samples, stable DC offset at −8 throughout. The
4-mic's quiet floor gives Silero a clear silence→speech edge that the
confidence accumulator in `SileroVADAnalyzer` can integrate past
`start_secs`. This confirmed the bug is specific to the 2-mic's signal
characteristics (amplitude saturation, DC wander) interacting with Pipecat's
VAD wrapper — not the pipeline wiring, gating, or ONNX runtime.

**Analysis:** `4mic_ab_comparison.md`

---

## Dump 3 — 2-mic WM8960, harness (standalone VAD probe)

| Field | Value |
|---|---|
| Files | `~/harness_dump_20260411_113046.pcm`, `.meta` |
| Date | 2026-04-11 11:30 |
| HAT | 2-mic WM8960 (`seeed-2mic-voicecard`) |
| Size | 219 520 bytes |
| Duration | 6.9 s (343 frames × 20 ms) |
| Format | 16 kHz, mono, int16 LE |
| Command | `LOG_LEVEL=DEBUG PIPELINE_FRAME_DUMP=1 python mvp-modules/vad-only/vad_harness.py` |

**Narrative.** Harness-side frame dump on the 2-mic HAT after reboot, with
USB charger cord moved away from the Pi (was adjacent during dump 1). The
harness runs the same Pipecat `LocalAudioTransport` → `FrameDumpProcessor`
→ `SileroVADAnalyzer` path but without OWW, phase gating, SHM ring, or
IPC. Different session from dump 1, so the comparison is signal profile,
not byte-level.

The signal is markedly cleaner than dump 1 despite being the same HAT and
default ALSA gain: median frame RMS 780 (vs 1 702), zero clipping (vs 130),
DC wander ±367 (vs ±860). Two changes between the dumps: (a) USB cord moved
away, (b) fresh reboot. The startup transient is near-identical (−24 623 vs
−24 494), confirming it's a hardware constant unaffected by EMI.

The critical difference: 34 % of frames below 500 RMS (near-silence) vs
only 9 % in dump 1. This dynamic range gives Silero clear silence→speech
edges. The harness VAD fires; the pipeline VAD on dump 1 did not. Whether
the improvement comes from the cord move (EMI reduction) or speech
content/distance is not yet isolated — a pipeline dump with the cord moved
(dump 4) would separate the variables.

**Analysis tool:** `analyze_dump.py` (run across all dumps for
reproducible metrics).

---

## Dump 4 — 2-mic WM8960, pipeline, USB cord moved (VAD fires)

| Field | Value |
|---|---|
| Files | `~/pipeline_dump_20260411_113946.pcm`, `.meta` |
| Date | 2026-04-11 11:39 |
| HAT | 2-mic WM8960 (`seeed-2mic-voicecard`) |
| Size | 960 000 bytes |
| Duration | 30.0 s (1 500 frames × 20 ms, hit cap) |
| Format | 16 kHz, mono, int16 LE |
| Command | `LOG_LEVEL=DEBUG PIPELINE_FRAME_DUMP=1 python assistant/voice_assistant.py` |

**Narrative.** The variable-isolation dump: same full pipeline as dump 1,
same 2-mic HAT, but USB charger cord moved away from the Pi. **VAD fired.**
This is the first time Silero VAD has fired in the full recorder pipeline
on the 2-mic HAT.

The signal profile is very close to dump 1 in shape — median frame RMS
1 542 (vs 1 702), 69 % of frames above 1 000 RMS (vs 71 %), only 9 % below
500 (vs 9 %). The WM8960 is still hot. But clipping dropped from 130
samples to just 2, and DC wander is comparable (±458/+770 vs ±860).

**Key comparison — what changed vs dump 1:**

| Metric | Dump 1 (cord adj.) | Dump 4 (cord moved) |
|---|---|---|
| VAD fired? | **No** | **Yes** |
| Median frame RMS | 1 702 | 1 542 |
| Frames RMS > 2 000 | 42.0 % | 34.9 % |
| Clipped samples | 130 | 2 |
| DC range | −860 to +619 | −458 to +770 |
| Startup frame 0 mean | −24 494 | −24 160 |

The amplitude reduction is modest (~10 % median RMS), and the signal is
still much hotter than the 4-mic (dump 2, median 34) or even the harness
(dump 3, median 780). Yet VAD fires. This suggests the clipping (130 → 2
samples) was the critical factor — a few hard-clipped frames may corrupt
Silero's LSTM state or prevent the Pipecat confidence accumulator from
crossing `start_secs`, while the merely-hot-but-unclipped signal in dump 4
gives Silero enough dynamic information to detect transitions.

**Spectral note:** 67.5 % of energy below 100 Hz is anomalous — likely
dominated by the DC wander over 30 s of capture. Speech-band content
(300–3 kHz) is 22 %, lower than dump 1's 58 % but the dump is 2× longer
with more silence, diluting the speech fraction.

**Analysis tool:** `analyze_dump.py`

---

## Current state

Rebooted. 2-mic HAT installed. USB charger cord moved away from HAT.
**VAD fires in the full pipeline** (dump 4). The USB cord proximity was
the root cause — likely EMI coupling into the WM8960's analogue front-end,
pushing the signal into clipping that prevented Silero from detecting
speech transitions.

**Resolved variable isolation:**

| Dump | Pipeline? | Cord adjacent? | VAD fires? |
|---|---|---|---|
| 1 | Yes | Yes | No |
| 2 | Yes | — (4-mic) | Yes |
| 3 | Harness | No | Yes |
| 4 | Yes | No | **Yes** |

The cord position, not harness-vs-pipeline, was the differentiator.

---

## Disposition

### Learnings

EMI root cause and hardware rule documented in
`mvp-modules/memory/pipecat_learnings.md` § "EMI and Microphone Signal
Integrity".

### Input quality monitor

`assistant/input_quality.py` — `InputQualityProcessor`, env-gated by
`INPUT_QUALITY_CHECK=1`. Accumulates per-frame RMS, clipping count, and
DC offset over a configurable window (default 5 s), then emits a one-time
WARNING if the profile matches dump-1 conditions. Wired into both the
recorder pipeline (`recorder_process.py`) and the VAD harness
(`vad_harness.py`).

Threshold validation against all four dumps:

| Dump | pct > 2 000 | Clipped | DC span | Verdict |
|---|---|---|---|---|
| 1 (broken) | 42 % | 130 | 1 479 | **WARNING** |
| 2 (4-mic) | 2.6 % | 0 | 104 | OK |
| 3 (harness) | 19.2 % | 0 | 700 | OK |
| 4 (pipeline, cord moved) | 34.9 % | 2 | 1 228 | OK |

Thresholds: ≥ 40 % frames RMS > 2 000, ≥ 10 clipped samples,
DC span ≥ 1 400 (any one triggers).

### Analysis tool

`mvp-modules/vad-only/analyze_dump.py` — reusable PCM analysis script.
Takes one or more `.pcm` paths, prints per-dump reports and a side-by-side
comparison table.

### Open item

The 2-mic WM8960 signal is still "hot" (median RMS ~1 500) even with the
cord moved. Whether this needs further treatment (ALSA gain reduction, DC
HP filter, software AGC) for robust long-term operation is a separate
question — see item 3.
