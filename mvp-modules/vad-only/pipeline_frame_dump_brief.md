# Brief: Pipeline Frame Dump Processor

**Purpose:** Inline diagnostic tap that writes raw PCM from inside the Pipecat
pipeline to a file on disk, so we can observe exactly what audio reaches
downstream processors (VAD, OWW, ring writer) in the recorder child.

**Motivation:** VAD works in the standalone harness (`vad_harness.py`) but not
in the full recorder pipeline after migrating from the 4-mic (AC108) to 2-mic
(WM8960) HAT. The audio path from ALSA through Pipecat's `LocalAudioTransport`
into the pipeline is a black box — this processor makes it observable.

---

## Design

### New file

`assistant/frame_dump.py` — single module, one `FrameProcessor` subclass.

### Class: `FrameDumpProcessor(FrameProcessor)`

- **Activation:** env var `PIPELINE_FRAME_DUMP=1`. When unset or `0`, the
  processor is not composed into the pipeline (same conditional pattern as
  `DutyCycleEntry`/`DutyCycleExit`).
- **Separate concern** from duty cycle instrumentation. Duty cycle measures
  timing; this captures content.

### Behaviour

1. On first `InputAudioRawFrame`, open two files:
   - `~/pipeline_dump_<YYYYMMDD_HHMMSS>.pcm` — raw int16 PCM bytes
   - `~/pipeline_dump_<YYYYMMDD_HHMMSS>.meta` — text sidecar with:
     ```
     sample_rate=16000
     channels=1
     sample_width=2
     format=int16le
     ```
     (Values read from the first frame's attributes, not hardcoded.)

2. On every `AudioRawFrame` / `InputAudioRawFrame`: write `frame.audio` bytes
   to the PCM file. No transformation, no buffering — raw append.

3. **Cap:** stop writing after `MAX_DUMP_SECS` seconds of audio (default 30).
   At 16 kHz mono int16 that's ~960 KB. Log a message when the cap is hit.
   Keep the processor in the pipeline (it still pushes frames) but stop writing.

4. On pipeline shutdown (CancelFrame or processor cleanup): close the file
   handle, log the final file path and byte count.

5. **Frame passthrough:** always `await self.push_frame(frame, direction)` for
   every frame type. This processor is read-only — it must not alter, drop, or
   delay any frame.

### Pipeline composition

In `recorder_child_main()`, insert conditionally immediately after
`input_transport` (before `DutyCycleEntry` if present, before
`GatedVADProcessor`). This captures audio as close to the transport as possible.

```python
processors = [input_transport]
if frame_dump_enabled:
    processors.append(FrameDumpProcessor())
if duty_collector:
    processors.append(DutyCycleEntry(duty_collector))
processors.extend([vad_processor, wake_processor, audio_writer])
if duty_collector:
    processors.append(DutyCycleExit(duty_collector))
```

Activation check:

```python
frame_dump_enabled = os.environ.get("PIPELINE_FRAME_DUMP", "0") == "1"
```

### What to log (loguru, not the dump file)

- On first frame: file path, frame metadata (sample_rate, num_channels, len(audio))
- Every 50th frame: frame count, bytes written so far (TRACE level)
- On cap hit: warning with total bytes and seconds
- On close: final byte count and file path

---

## Verification

After implementing, run the recorder with `PIPELINE_FRAME_DUMP=1` and confirm:

1. `.pcm` and `.meta` files appear in `~/`
2. The PCM file can be imported into Audacity (File → Import → Raw Data,
   signed 16-bit LE, mono, 16000 Hz) or played with:
   ```bash
   aplay -f S16_LE -r 16000 -c 1 ~/pipeline_dump_*.pcm
   ```
3. Compare against a `smoke_capture.py` recording from the same session —
   amplitude and content should match if the pipeline is passing audio cleanly.
4. If they don't match, the delta is the bug.

---

## Reference files

| File | Role |
|---|---|
| `assistant/recorder_process.py` | Pipeline composition site (lines 639-644) |
| `assistant/recorder_process.py` | `DutyCycleEntry` — pattern reference for conditional processor |
| `mvp-modules/vad-only/vad_harness.py` | Known-good VAD path (fires correctly on 2-mic audio) |
| `mvp-modules/2-mic/smoke_capture.py` | Known-good capture (reference amplitude) |
| `mvp-modules/2-mic/README.md` | 2-mic HAT hardware summary, mono downmix strategy |
