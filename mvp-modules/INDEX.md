# MVP Modules — Index

End-to-end voice pipeline development for Raspberry Pi 4 (`morpheus`), using Pipecat, openWakeWord, Silero VAD, and Deepgram Nova-3. Evolving toward a two-process "forked assistant" architecture with Claude agentic layer.

## Project Definition

- **`starting_brief.md`** — PoC brief: full pipeline (wake word → VAD → STT → agentic layer → TTS), build sequence (steps 1–9), pass/fail criteria, latency targets.

## Active Development

- **`forked_assistant/`** — Two-process recorder architecture. Recorder child (core 0) owns the microphone and Pipecat pipeline. Master (cores 1–3) runs STT, Claude, and response handling. See `forked_assistant/AGENTS.md` for full context.

## Deliverables (concluded steps 4–6)

| File | Step | What it does |
|------|------|--------------|
| `deliverables/voice_pipeline_minimal.py` | 4 | Wake word detection only (fires, prints, exits) |
| `deliverables/voice_pipeline_step5.py` | 5 | + VAD / utterance capture |
| `deliverables/voice_pipeline_step6.py` | 6 | + Deepgram STT, clean Ctrl+C exit, no buffer overflow |
| `deliverables/step_6_delivery.md` | 6 | Delivery report: acceptance criteria, bugs found, Pipecat learnings |

## Structured Knowledge

| File | Purpose |
|------|---------|
| `memory/pipecat_learnings.md` | Pipecat API rules proven across 30+ iterations |
| `memory/shutdown_and_buffer_patterns.md` | Root causes, proven fixes, anti-patterns for audio pipeline stability |
| `memory/architecture_decisions.md` | Rationale for two-process design, SharedMemory+Pipe, VAD-as-sensor, PipelineState |

## Archives (concluded efforts)

| Directory | Effort | Date | Outcome |
|-----------|--------|------|---------|
| `archive/step6/` | Wake + capture + STT pipeline | 2026-03-31 | Success. Delivered `voice_pipeline_step6.py` |
| `archive/step7/` | Agentic layer (single-process) | 2026-03-31 to 2026-04-01 | Operational path works; shutdown crash unresolvable in single process. Pivoted to `forked_assistant/` |

Each archive has its own `AGENTS.md` with context on what was learned and why it's archived.

## Agentic Context

- **`AGENTS.md`** (this directory) — Always-loaded context: project identity, constraints, naming conventions, directory structure.
- **`archive/step6/AGENTS.md`** — Step 6 archive context.
- **`archive/step7/AGENTS.md`** — Step 7 archive context.
- **`forked_assistant/AGENTS.md`** — Active development context: EU phasing, hard constraints, current state.

## Naming Convention

- **Time-bound** artifacts have a date prefix (`2026-03-31_filename`) reflecting applicability to a point in time.
- **Durable** artifacts lack a date prefix and have continuing relevance.
