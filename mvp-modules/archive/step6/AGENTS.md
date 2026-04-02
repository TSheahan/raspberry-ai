# archive/step6 — Agent Context

## What This Archive Contains

Step 6 of the Pipecat PoC: building a wake word + utterance capture + Deepgram STT pipeline. This effort ran on 2026-03-31 and concluded successfully. The deliverable is `deliverables/voice_pipeline_step6.py`.

## Why It's Archived

All acceptance criteria were met. The pipeline pattern is proven and carries forward into subsequent work. This archive preserves the full investigation history for reference — it is not active development.

## Key Findings (extracted to memory)

Two bugs were found and resolved:

1. **Missing `process_frame` override** — Pipecat's base `FrameProcessor.process_frame()` does not push frames downstream. Any subclass without an override silently swallows all frames, including `StartFrame` and `CancelFrame`. This caused shutdown hangs that were misdiagnosed through 8 rounds of troubleshooting before incremental isolation identified it as a single-variable root cause.

2. **Buffer overflow from `np.append`** — `np.append(buffer, chunk)` is O(n) per call and caused ALSA underruns leading to USB device hangs and Pi reboots. Fixed by switching to `list.append` + deferred `np.concatenate`.

Full details in `memory/pipecat_learnings.md` and `memory/shutdown_and_buffer_patterns.md`.

## Archive Structure

- `README.md` — guided tour of the investigation
- `2026-03-31_progression_story.md` — narrative of v1–v8 troubleshooting
- `2026-03-31_shutdown_iteration_guide.md` — methodology, root cause analysis, change audit
- `2026-03-31_first_attempt.py` — original script
- `2026-03-31_diagnostic_minimal*.py` — non-Pipecat baseline series
- `2026-03-31_pipecat_shutdown_v*.py` — failed troubleshooting series (v1–v8)
- `2026-03-31_incremental_v*.py` — systematic isolation series (v1–v10, the method that found root cause)
- `observations/` — raw terminal output from test runs
