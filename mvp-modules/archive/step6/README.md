# Step 6 Worklog

Working files from the Step 6 shutdown/pipeline investigation. The deliverable is `../voice_pipeline_step6.py` (a copy of `incremental_v10.py`).

## Key documents

- **step_6_shutdown_iteration_guide.md** — The primary reference. Contains root cause analysis (missing `process_frame` override), buffer overflow analysis, the full change audit (wanted vs. jitter), and the complete test progression.
- **step_6_progression_story.md** — Narrative history of the v1-v8 troubleshooting attempts before the incremental isolation strategy.
- **observations/** — Raw terminal output from test runs.

## Script series

### 1. First attempt

`first_attempt.py` — Original Pipecat pipeline. Had `process_frame` on all processors (correct), but used inline transcription during frame processing (racey), manual `KeyboardInterrupt` catch, and `asyncio.get_event_loop().time()`.

### 2. Pipecat shutdown series (v1-v8) — failed troubleshooting

Monolithic attempts to fix the shutdown hang. Each added more scaffolding (custom signal handlers, global variables, force-close PyAudio internals, TRIPWIRE debug prints). v8 cleaned up all the jitter but accidentally removed `process_frame` from `DeepgramSTTProcessor`, which was the actual root cause of the hang.

- `pipecat_shutdown_v1.py` through `pipecat_shutdown_v8.py`

### 3. Diagnostic series — isolation baseline

Stripped Pipecat out entirely. Direct PyAudio to prove clean exit was possible. v4 became the baseline for the incremental rebuild.

- `diagnostic_minimal.py` through `diagnostic_minimal_v4.py`

### 4. Incremental series (v1-v10) — systematic isolation

Started from the working diagnostic baseline and re-introduced Pipecat components one at a time.

| Script | What it tested | Result |
|--------|---------------|--------|
| `incremental_v1.py` | Baseline confirmation (diagnostic v4 copy) | PASS |
| `incremental_v2.py` | Minimal Pipecat framework, RTVI off | PASS |
| `incremental_v2a.py` | RTVI on | PASS |
| `incremental_v3.py` | + OpenWakeWordProcessor | PASS |
| `incremental_v4.py` | + UtteranceCapturer | PASS |
| `incremental_v5.py` | + DeepgramSTTProcessor (full feature set) | PASS |
| `incremental_v6.py` | Import timing alignment (match v8) | PASS |
| `incremental_v7.py` | Class definition order (match v8) | PASS |
| `incremental_v8a.py` | Method rename + process_frame removal + comment | FAIL |
| `incremental_v8b.py` | process_frame removal only | FAIL |
| `incremental_v9.py` | Clean production script (first_attempt + wanted changes) | PASS |
| `incremental_v10.py` | Buffer overflow fix | PASS |

v8a/v8b isolated the root cause: `FrameProcessor.process_frame()` does not push frames downstream. Every subclass must override it and call `push_frame`. See `step_6_shutdown_iteration_guide.md` Section 1a.
