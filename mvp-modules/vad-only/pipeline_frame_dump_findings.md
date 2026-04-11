# Findings: Pipeline Frame Dump — 2-mic WM8960 HAT

**Companion to:** `pipeline_frame_dump_brief.md`
**Date:** 2026-04-11
**Dump file:** `~/pipeline_dump_20260411_104958.pcm` (435 840 bytes, 13.6 s)

---

## Summary

The audio path from ALSA through Pipecat `LocalAudioTransport` into the
pipeline is **healthy**. Real speech reaches every downstream processor with
correct amplitude and spectral content. The frame dump rules out the audio
path as the cause of the VAD-not-firing bug.

STT (Deepgram, reading from the SHM ring downstream of VAD) continues to
transcribe correctly, confirming the same conclusion from the other end.

---

## What the dump shows

### Audio is real and well-formed

- 681 clean 20 ms frames, zero remainder — frame boundaries are intact.
- Spectral energy concentrated in the speech band:
  52 % in 300–3 000 Hz, 20 % in 100–300 Hz.
- Dominant peaks at ~435 Hz and ~576 Hz (voice fundamentals).
- 42 % of frames have RMS > 2 000 (active speech); only 9 % below 500
  (near-silence). Natural speech-then-pause pattern visible in the energy
  heatmap.

### Codec startup transient

The first ~100 ms contains a large DC swing (frame 0 mean ≈ −24 500,
decaying exponentially to near-zero by frame 5). This is the WM8960's
coupling-capacitor charge-up. It presents as a loud pop to any processor
that sees the first frames of a new stream open.

### Wandering DC offset

Per-second DC mean drifts between −860 and +619 throughout the capture.
Normal for a single-ended MEMS mic path without hardware HP filter.

### Minor observations

| Finding | Severity | Detail |
|---|---|---|
| 4-sample stutter at byte 0 | Cosmetic | First two quads duplicated; ALSA buffer priming artifact. 2 of 54 K quads. |
| 131 clipped samples (±32 767) | Low | 0.06 % of samples, concentrated in loud bursts at 7–10 s. Mic gain slightly hot for close-talk. |
| Capture stopped at 13.6 s | Expected | Pipeline shut down before the 30 s cap — matches Ctrl-C / phase change in session. |

---

## Approaches tried and struck out

| # | Approach | Outcome |
|---|---|---|
| 1 | `vad_harness.py` standalone (same 2-mic HAT, same ALSA device) | VAD fires correctly — speech_started / speech_stopped as expected. |
| 2 | Pipeline frame dump (this analysis) | Audio reaching VAD is real speech with good amplitude. Rules out the audio path. |
| 3 | `min_volume=0.0` (disabled volume gate in `VADParams`) | No change — VAD still silent inside the pipeline. |

The bug is **not** in the audio content. It is somewhere in how Silero VAD
is invoked or how its output propagates inside the Pipecat pipeline context.

---

## Remaining hypotheses

1. **Phase gating masks the problem.** `GatedVADProcessor` only forwards
   audio to `VADController` during `capture`. If OWW never fires (or fires
   and the state machine never reaches `capture`), VAD inference is simply
   never called. The frame dump cannot distinguish this — it captures all
   phases.

2. **Pipecat VADController internal state.** The controller receives a
   `StartFrame` once at pipeline start, then audio frames only when gated
   in. If the controller expects continuous audio to maintain its internal
   ring / confidence accumulator, intermittent feeding (only during
   `capture`) could prevent it from ever crossing the `start_secs`
   threshold.

3. **Silero ONNX session interaction with OWW.** Both use ONNX Runtime on
   the same core (core 0, SCHED_FIFO). The drain guard serialises them at
   phase boundaries, but within `capture` Silero runs synchronously in
   `process_frame` while OWW's `_pending_predict` may still be completing
   from the prior phase. A subtle ordering or thread-pool contention issue
   could stall inference.

4. **Hardware-specific Silero behaviour.** Silero VAD's ONNX model may
   behave differently on armv8 / aarch64 vs x86 in edge cases (quantisation,
   intermediate precision). The harness works because it calls Silero
   directly; the pipeline wraps it in Pipecat's VADController which adds
   buffering and state tracking.

---

## Next step: swap to 4-mic HAT and repeat

The 4-mic ReSpeaker (AC108 codec) was the original hardware where VAD
worked in the full pipeline. Repeating the frame dump on the 4-mic HAT
creates a direct A/B comparison:

- **If VAD fires on 4-mic:** the bug is specific to 2-mic audio
  characteristics (DC offset, amplitude, frame timing) interacting with
  Silero/Pipecat — not the pipeline wiring.
- **If VAD still doesn't fire on 4-mic:** the bug was introduced in a code
  change concurrent with the HAT migration, and the HAT swap was a red
  herring.

### Procedure

1. Power down the Pi.
2. Physically swap the 2-mic WM8960 HAT for the 4-mic ReSpeaker HAT.
3. Boot, verify `arecord -l` shows card 1 as `seeed-4mic-voicecard`.
4. Run with frame dump:
   ```bash
   PIPELINE_FRAME_DUMP=1 python assistant/voice_assistant.py
   ```
5. Speak a wake phrase + utterance, wait for VAD cycle, Ctrl-C.
6. Analyse the new dump with the same metrics (DC offset, spectral
   energy, transient, RMS profile) and compare against this file.

### What to record

- Does OWW detect the wake word? (check recorder logs)
- Does the state machine reach `capture`?
- Does Silero VAD fire `speech_started` / `speech_stopped`?
- Frame dump metrics: DC offset, RMS distribution, spectral band energy.

---

## Reference

| File | Role |
|---|---|
| `~/pipeline_dump_20260411_104958.pcm` | This dump (2-mic WM8960, 13.6 s) |
| `~/pipeline_dump_20260411_104958.meta` | Sidecar: sr=16000, ch=1, sw=2, int16le |
| `assistant/frame_dump.py` | FrameDumpProcessor implementation |
| `assistant/recorder_process.py` | Pipeline composition (lines 641–648) |
| `mvp-modules/vad-only/vad_harness.py` | Standalone harness where VAD works |
| `mvp-modules/vad-only/pipeline_frame_dump_brief.md` | Original brief |
