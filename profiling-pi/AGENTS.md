# profiling-pi — Agent Context

## What This Is

Declarative provisioning profiles for the Raspberry Pi 4 (morpheus). Each markdown
file captures one concern: what a correctly provisioned state looks like, how to
reach it, and how to verify it. Together, these files are the reproducible record
of the Pi's runtime environment.

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
where possible (re-running does not break an already-provisioned system).

### Verify (observable)

Commands that confirm the target state was reached. Each verification step includes
the expected output. An agent can run these non-destructively at any time to audit
the current state against the profile.

## Separation of Concerns

Each file owns exactly one provisioning domain:

| File | Domain |
|------|--------|
| `venv.md` | Python virtual environment: package installs, version pins, platform notes |
| `agent-user-setup.md` | Dedicated `agent` Linux user, sudoers, Cursor CLI, workspace |
| `piper-tts-setup.md` | Piper TTS engine and voice model (archived — Piper rejected for OOM) |
| `pip-hardening.md` | pip cooldown wrapper and supply-chain protections |
| `priority-permissions.md` | Real-time scheduling, CPU pinning, and process priority |

New profiles should follow the same three-section pattern and own a single concern.

## Key Constraints

- **Target hardware:** Raspberry Pi 4 Model B, 1 GB RAM, quad-core ARM Cortex-A72
- **OS:** Raspberry Pi OS Trixie (Debian 13), ALSA only (no PulseAudio/PipeWire)
- **Python venv:** `~/pipecat-agent/venv/`
- **Execution user:** `user` (voice assistant processes); `agent` (Cursor CLI subprocess)
