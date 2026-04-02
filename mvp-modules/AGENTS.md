# mvp-modules — Agent Context

## Project Identity

This is the MVP development area for a Raspberry Pi 4 voice assistant ("morpheus"). The goal is an always-on voice pipeline: wake word → utterance capture → STT → agentic layer (Claude) → TTS → audio output, running on a Pi 4 with a ReSpeaker 4-Mic Array.

The project originated as a Pipecat PoC (`starting_brief.md`, steps 1–9). Steps 1–6 concluded successfully (single-process pipeline through STT). Step 7 (agentic layer) concluded with an unresolvable single-process shutdown crash, leading to the current effort: a two-process "forked assistant" architecture in `forked_assistant/`.

## Repository Context

This repo lives on Windows for Cursor IDE availability and is checked out on the development Pi for execution. Code runs on ARM64 Linux (Raspberry Pi OS Trixie, ALSA only, no PulseAudio/PipeWire).

## Directory Structure

```
mvp-modules/
├── AGENTS.md              ← you are here
├── INDEX.md               ← living map of all content
├── starting_brief.md      ← project definition (steps 1–9)
├── memory/                ← structured knowledge for purpose-specific recall
├── deliverables/          ← concluded step outputs (steps 4–6)
├── archive/               ← concluded development efforts
│   ├── step6/             ← wake+capture+STT pipeline (concluded 2026-03-31)
│   └── step7/             ← agentic layer attempt (concluded 2026-04-01)
└── forked_assistant/      ← ACTIVE: two-process recorder architecture
    ├── spec/              ← design specifications
    ├── src/               ← library code (ring_buffer, recorder_state)
    ├── test/              ← harnesses and smoke tests
    └── archive/           ← superseded snapshots
```

## Naming Convention: Time-bound vs Durable

- **Time-bound** artifacts are prefixed with an ISO timestamp to reflect applicability to a point in time. They capture session state, iteration history, observations, and analysis from concluded efforts.
  - Date precision is sufficient when one artifact per day: `2026-03-31_filename`
  - Time precision is used when multiple artifacts land on the same day and ordering within the day matters: `2026-04-02T1400_filename`, `2026-04-02T1401_filename`
  - The prefix format is `YYYY-MM-DD` or `YYYY-MM-DDTHHMM` — no seconds, no timezone suffix
- **Durable** artifacts lack a date prefix and have continuing relevance. Specs, library code, deliverables, and active design docs are durable.

## Key Constraints (always apply)

- **Target hardware:** Raspberry Pi 4 Model B, quad-core ARM Cortex-A72
- **Audio:** ReSpeaker 4-Mic Array (input, device index 1), bcm2835 headphones (output, device index 0), ALSA only
- **Python venv:** `~/pipecat-agent/venv/` on the Pi
- **Pipecat version:** 0.0.108 — `process_frame` must be overridden in every `FrameProcessor` subclass (see `memory/pipecat_learnings.md`)
- **openWakeWord:** 0.4.0 exactly — 0.6 breaks the shared venv
- **No concurrent ONNX:** OWW and Silero VAD must run in non-overlapping phases; Pi 4 cannot sustain both

## What's Active

`forked_assistant/` is the active development frontier. It implements a two-process architecture (recorder child on core 0, master on cores 1–3) to solve the shutdown crash that blocked step 7. See `forked_assistant/AGENTS.md` for detailed context.

## Memory Files

Purpose-specific knowledge lives in `memory/`. Read the relevant file when working in that domain:
- `pipecat_learnings.md` — Pipecat API rules and patterns proven across 30+ iterations
- `shutdown_and_buffer_patterns.md` — root causes, proven fixes, anti-patterns for audio pipeline stability
- `architecture_decisions.md` — rationale for key design choices in the two-process architecture
