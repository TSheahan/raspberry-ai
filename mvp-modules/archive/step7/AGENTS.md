# archive/step7 — Agent Context

## What This Archive Contains

Step 7 of the Pipecat PoC: adding the agentic layer (Claude subprocess) to the proven wake+capture+STT pipeline. This effort ran 2026-03-31 through 2026-04-01 and concluded with a known unresolvable issue in the single-process architecture.

## Why It's Archived

The operational path works — wake detection, VAD gating, STT, Claude response all function correctly. However, the shutdown path crashes the Pi (hard reboot) after any completed cognitive loop. The crash occurs in PortAudio's C layer during teardown when PyAudio's callback thread races against asyncio cancellation. This is beyond Python-level control in a single process.

This finding triggered the design pivot to `forked_assistant/` (two-process architecture), which is the active development effort.

## Key Findings (extracted to memory)

1. **CPU starvation causes USB audio cascade** — Claude subprocess (7–12s) + any ONNX inference = unbounded queue growth → USB isochronous transfer starvation → kernel panic or USB host controller reset.

2. **Both ONNX workloads must be phase-gated** — OWW in LISTENING, Silero in CAPTURING, neither during PROCESSING. This is architectural, not optional.

3. **OWW 5-buffer reset required on ungating** — Without resetting `prediction_buffer`, `raw_data_buffer`, `melspectrogram_buffer`, `feature_buffer`, and `accumulated_samples`, stale features produce false-positive wake detections (observed score: 0.865).

4. **PipelineState pattern (v11)** — Centralizing shared state into a single object with phase-transition side-effects eliminated cross-processor reference tangles. This pattern carries forward into the two-process design.

5. **Stream pause during cognitive loop** — Stopping PyAudio input during Claude processing reduces USB contention. Silero LSTM states must be reset before stream resume.

## Archive Structure

- `2026-03-31_pickup_step_7_notes.md` / `2026-04-01_*.md` — session notes and guides
- `2026-04-01_crash_analysis.md` — consolidated crash investigation
- `2026-04-01_state_breakout_v10_to_v11.md` — PipelineState refactor analysis
- `2026-03-31_voice_pipeline_step7_v01.py` through `2026-04-01_*_v11.py` — versioned pipeline scripts
- `increments/` — patch recipes (v01a–v02)
- `observations/` — terminal captures from test runs

## Relationship to forked_assistant

The two-process architecture in `forked_assistant/` directly inherits:
- The phase-gating pattern from v01b/v02
- The PipelineState object from v11
- The OWW reset protocol from v03
- The stream lifecycle management from v10a/v11
- The crash analysis findings that motivated process separation
