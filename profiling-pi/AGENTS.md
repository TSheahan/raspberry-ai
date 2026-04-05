# profiling-pi — Agent Context

## What This Is

Declarative provisioning profiles for the Raspberry Pi 4 (morpheus). Each markdown
file captures one concern: what a correctly provisioned state looks like, how to
reach it, and how to verify it. Together, these files are the reproducible record
of the Pi's runtime environment.

## Provisioning Order

Start with `voice-user-setup.md`. It creates the `voice` Linux user, clones the
repo, and sets up the venv. Everything else depends on it — the repo must be
checked out before any other profile can be read or executed on the Pi.

Steps 1–4 are the critical path to agentic execution on the Pi. Once the Cursor
CLI agent is running on-device, it can read these profiling docs and execute the
remaining steps itself — the provisioning becomes self-accelerating. Getting to
step 4 quickly is worth front-loading.

1. **`voice-user-setup.md`** — create `voice` user, home layout, repo clone, venv
2. **`pip-hardening.md`** — lock down pip/uv before installing further packages
3. **`venv.md`** — install and verify each Python dependency
4. **`agent-user-setup.md`** — dedicated `agent` user for Cursor CLI subprocess
5. **`priority-permissions.md`** — real-time scheduling for the recorder child
6. **`piper-tts-setup.md`** — Piper ONNX model (archived — too resource-intensive)

## Structure

Each profiling file follows a three-section pattern:

### Target State (descriptive)

A declarative description of what a satisfactorily provisioned system exhibits.
Written as observable properties — package installed, config file present, service
running, command returns expected output. An agent or operator can read this
section and determine "am I already done?" without executing anything.

### Instructions (imperative)

Step-by-step commands to reach the target state from a clean baseline. Numbered,
copy-pasteable, with prerequisites stated up front. Each step should be idempotent
where possible (re-running does not break an already-provisioned system). Agentic
instruction (natural language directed at an agent) is acceptable here when it is
a better fit than a shell command — these files are for agents most of all.

### Verify (observable)

Commands that confirm the target state was reached. Each verification step includes
the expected output. An agent can run these non-destructively at any time to audit
the current state against the profile.

## Separation of Concerns

Each file owns exactly one provisioning domain:

| File | Domain |
|------|--------|
| `voice-user-setup.md` | `voice` Linux user, home layout, repo checkout, venv creation |
| `venv.md` | Python dependency profiles: install, verify, platform notes |
| `agent-user-setup.md` | Dedicated `agent` Linux user, sudoers, Cursor CLI, workspace |
| `piper-tts-setup.md` | Piper TTS engine and voice model (archived — too resource-intensive) |
| `pip-hardening.md` | pip cooldown wrapper and supply-chain protections |
| `priority-permissions.md` | Real-time scheduling, CPU pinning, and process priority |

**Related (not a profiling profile):** agent CLI wrapper design handoff lives in `mvp-modules/forked_assistant/archive/2026-04-06_agent_wrapper_handoff_brief.md` (Pi provisioning for that work extends `profiling-pi` when implemented).

New profiles should follow the same three-section pattern and own a single concern.
Not all existing files fully conform to this structure yet — they should be
updated to converge toward it over time.

## Key Constraints

- **Target hardware:** Raspberry Pi 4 Model B, 1 GB RAM, quad-core ARM Cortex-A72
- **OS:** Raspberry Pi OS Trixie (Debian 13), ALSA only (no PulseAudio/PipeWire)
- **Python venv:** `/home/voice/venv/`
- **Execution user:** `voice` (voice assistant processes); `agent` (Cursor CLI subprocess)
