# Privilege Separation Analysis — Agent Subprocess User Isolation

**Date:** 2026-04-04
**Scope:** How to run the Cursor CLI agent subprocess as a dedicated `agent` Linux user while master.py runs as `user`
**Decision:** Option B (`sudo -u agent -H`) — see §4

---

## Problem Statement

`CursorAgentSession.prepare()` spawns the Cursor CLI via `subprocess.Popen`. As written, the subprocess inherits master's UID (`user`). `user` is a sudoer. The agent runs with `--force --yolo`, bypassing tool confirmation. This combination means any bash tool invocation by the agent can run `sudo <anything>` without a password — an unacceptably wide blast radius, even on an appliance Pi.

Three options were evaluated:

- **A** — Root launch (master starts as root, uses `Popen(user='agent')`, drops privileges)
- **B** — `sudo -u agent -H` prefix in Popen command (master stays as `user`)
- **C** — No separation (agent subprocess runs as `user`)

---

## Why Option C Is Ruled Out

`user` is a sudoer. With `--yolo` active, a confused or prompt-injected agent can invoke bash tools that run `sudo systemctl ...`, `sudo apt install ...`, or `sudo rm -rf /` without a password prompt. `--workspace` confinement is a UX constraint on the agent's context window, not an OS-level sandbox — it does not restrict bash tool invocations. Option C is not a defensible baseline.

---

## Why Option A (Root Launch) Is Rejected

`Popen(user='agent')` requires root at the moment of each call. Since `prepare()` is called on every `WAKE_DETECTED` (every voice turn), master must remain root for its entire lifetime — a one-time privilege drop is not possible. An always-root master runs the ONNX pipeline, Deepgram SDK, and audio subsystem as root, with no security benefit over Option C (blast radius is root either way). Additionally:

- Recorder child forked from root → recorder child runs as root (unnecessary)
- ONNX model file loading as root (attack surface: malformed model files)
- Outbound WebSocket (Deepgram) as root (attack surface: malicious server response)
- SHM unlink in `finally` block: if root creates `/dev/shm/<SHM_NAME>`, `user` (after a drop) cannot unlink it due to sticky bit semantics

Option A2 (permanent root master) provides zero isolation advantage over Option C while adding operational risk and unnecessary privilege to the audio/ML subsystems.

---

## Option B: Chosen Approach

Master runs as `user`. The Popen command is prefixed with `sudo -u agent -H --`. A sudoers entry permits this without a password:

```
user ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent
```

The `--` after `-H` prevents sudo from parsing any subsequent flag-like argument (e.g. `--resume`) as its own option.

### Security Properties

- Master, recorder child, and all audio/ML code run as `user`
- Agent subprocess runs as `agent` (no sudo, no system-wide write, narrow home directory)
- `user` can execute exactly one binary path as `agent` — the sudoers entry is narrow and auditable
- `--force`/`--yolo` flags do not compound this: they bypass Cursor UI confirmation, not OS capabilities
- Blast radius of a misbehaving agent: `agent`'s home directory + `AGENT_WORKSPACE`
- `user` cannot escalate to `agent` permissions via any other path through this entry

### Operational Properties

- No changes to `main()`, SHM setup, recorder child spawning, or shutdown protocol
- `AGENT_USER=` unset in dev → code path falls through to direct invocation (no sudo)
- `AGENT_USER=agent` in Pi `.env` → sudo prefix active
- `sudo -u agent -H` sets `HOME=/home/agent` so the Cursor CLI finds `~agent/.cursor/` credentials
- Startup overhead of `sudo` on a NOPASSWD entry: ~5–30 ms per `prepare()` call; hidden behind the Deepgram streaming window

---

## Code Change

In `agent_session.py`, `CursorAgentSession.prepare()`:

```python
# Module level (alongside _DEFAULT_RESUME_WINDOW):
_AGENT_USER = os.environ.get("AGENT_USER", "")

# In prepare(), replace the flat cmd list with:
agent_args = [
    str(self._agent_bin),
    "-p",
    "--output-format", "stream-json",
    "--stream-partial-output",
    "--force",
    "--yolo",
    "--trust",
    "--workspace", str(self._workspace),
    "--model", self._model,
]
if resuming and self._session_id:
    agent_args.extend(["--resume", self._session_id])

cmd = (["sudo", "-u", _AGENT_USER, "-H", "--"] + agent_args) if _AGENT_USER else agent_args
```

`start_new_session=True` added to Popen: isolates the agent subprocess from terminal SIGINT (Ctrl+C does not propagate to the agent mid-response). `agent.close()` sends SIGTERM directly by PID, which is unaffected.

---

## Environment Variables

```
# ~/.env on the Pi
AGENT_USER=agent
AGENT_BIN=/home/agent/.local/bin/agent
AGENT_WORKSPACE=/home/agent/raspberry-ai
```

Pi provisioning steps (user creation, Cursor CLI install for `agent`, auth setup, sudoers entry, verification) are in `profiling-pi/agent-user-setup.md`.

---

## Verification

After WAKE_DETECTED with the agent user configured:

```bash
ps -eo user,pid,ppid,comm | grep agent
# agent  <pid>  <master-pid>  sudo
# agent  <pid2> <pid>         agent
```
