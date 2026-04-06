# Cursor agent wrapper — Pi provisioning

**Purpose:** Install and keep in sync the **cursor-agent-wrapper** described in `mvp-modules/forked_assistant/spec/cursor_agent_wrapper_spec.md`.  
**Depends on:** `voice-user-setup.md` (repo checkout under `/home/voice/`), `agent-user-setup.md` (Cursor CLI under `agent`).

**Locked choices (see spec §4, §5, Appendix C):** install **§4a only** (agent-home copy); wrapper **supervising parent (§5b)** — not thin `exec`.

---

## Target State

- Repo contains `agent-artifacts/cursor-agent-wrapper` (tracked, executable). **Agent-aligned** tree under the `voice` checkout; **runtime** copy lives under **`agent`** home only.
- **`/home/agent/artifacts/cursor-agent-wrapper`** exists: mode `0755`, owner `agent:agent`. Parent **`/home/agent/artifacts/`** exists with owner `agent:agent`, mode `0755`.
- Content matches the checkout after every `git pull` / `git merge` that changes the file (hook or manual installer).
- Sudoers: `voice` may run **only** that wrapper path **as agent**:  
  `voice ALL=(agent) NOPASSWD: /home/agent/artifacts/cursor-agent-wrapper`
- Sudoers: `voice` may run **only** the **fixed** installer ( **`voice` deposits into `agent`** — copy + chown, no arbitrary args):  
  `voice ALL=(ALL) NOPASSWD: /home/voice/raspberry-ai/agent-artifacts/scripts/install-cursor-agent-wrapper.sh`  
  (Adjust checkout path to match `voice-user-setup.md`.)
- **`/var/log/agent-wrapper.log`:** exists, `agent:agent`, mode **`0640`**, appendable by `agent`.
- `/home/voice/.env`: `AGENT_BIN=/home/agent/artifacts/cursor-agent-wrapper`.

---

## Logging permissions — wanted vs current (profiling coverage)

| Aspect | Wanted (target state) | Covered by this profile? | Gap / follow-up |
|--------|------------------------|---------------------------|-----------------|
| Log file exists before first assistant run | yes | **Yes** — bootstrap step 4 creates file | None |
| Owner / mode | `agent:agent`, **0640** | **Yes** — step 4 + Verify | None |
| Wrapper can append while running as `agent` | yes | **Yes** — matches effective UID of supervised child chain | **Validated** on Pi (2026-04-06 smoke test) |
| `logrotate` / retention | weekly (or similar), create new file with same owner/mode | **No** — not in Instructions yet | **Gap:** add `/etc/logrotate.d/agent-wrapper` (or document manual rotation). Until then, log may grow unbounded — acceptable for early Pi bring-up only |
| Audit “argv in logs” risk | low (transcript on stdin, not argv) | **Partially** — spec §6 + caller docs; no automated test | None for provisioning |
| Log file unwritable (spec §10) | Defined behaviour | **Yes** — wrapper falls back to **stderr only** for log lines and still spawns the child | Documented here (implementation choice) |

**Summary:** **File create + DACL** are fully specified and verifiable here. **Rotation** is the main **wanted-vs-current** gap; treat as the next profiling increment after the wrapper binary exists.

---

## Instructions

### 1. Add repo files (developer / in git)

- `agent-artifacts/cursor-agent-wrapper` — supervising launcher (§5b); bash, `setsid` with fd3 stdin preservation + `kill -TERM` to child process group, logging per spec §6.
- `agent-artifacts/scripts/smoke-wrapped-agent.py` — optional stdlib-only one-shot run: same subprocess shape as `master.py` / `CursorAgentSession`, without the voice stack.
- `agent-artifacts/scripts/install-cursor-agent-wrapper.sh` — must:
  - `mkdir -p /home/agent/artifacts`
  - `cp` wrapper from checkout → `/home/agent/artifacts/cursor-agent-wrapper`
  - `chown agent:agent` on file and directory (directory `0755`, file `0755`)
  - reference **fixed paths only**; no user-supplied arguments
- `agent-artifacts/scripts/post-merge-hook.sh` — dual-use: tracked git hook **and** manual deploy command. Calls the installer via `sudo -n`.

### 2. Install git hooks (as `voice`, once per clone)

From the repo root:

```bash
cd /home/voice/raspberry-ai
chmod +x agent-artifacts/scripts/install-cursor-agent-wrapper.sh
chmod +x agent-artifacts/scripts/post-merge-hook.sh
ln -sf ../../agent-artifacts/scripts/post-merge-hook.sh .git/hooks/post-merge
ln -sf ../../agent-artifacts/scripts/post-merge-hook.sh .git/hooks/post-checkout
```

The hook is a tracked script — symlinked, not copied — so it stays in sync with the repo.

If `sudo -n` fails, operator runs the same script manually after pull or local edit:

```bash
agent-artifacts/scripts/post-merge-hook.sh
```

### 3. Sudoers (as privileged user)

Replace or extend the agent entry from `agent-user-setup.md`:

```bash
# Remove standalone NOPASSWD to raw CLI if migrating fully to wrapper:
# voice ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent

echo 'voice ALL=(agent) NOPASSWD: /home/agent/artifacts/cursor-agent-wrapper' | \
  sudo tee /etc/sudoers.d/voice-assistant-agent-wrapper
sudo chmod 440 /etc/sudoers.d/voice-assistant-agent-wrapper
```

Add installer rule (**`voice` deposits to `agent`** via this single script):

```bash
echo 'voice ALL=(ALL) NOPASSWD: /home/voice/raspberry-ai/agent-artifacts/scripts/install-cursor-agent-wrapper.sh' | \
  sudo tee /etc/sudoers.d/voice-assistant-wrapper-install
sudo chmod 440 /etc/sudoers.d/voice-assistant-wrapper-install
```

Run `visudo -c` on each file under `/etc/sudoers.d/`.

### 4. Bootstrap install

As `voice`:

```bash
sudo -n /home/voice/raspberry-ai/agent-artifacts/scripts/install-cursor-agent-wrapper.sh
```

Create log file (as root):

```bash
sudo touch /var/log/agent-wrapper.log
sudo chown agent:agent /var/log/agent-wrapper.log
sudo chmod 640 /var/log/agent-wrapper.log
```

### 5. (Optional follow-up) logrotate

**Gap closure:** add a drop-in, e.g. `/etc/logrotate.d/agent-wrapper`:

```
/var/log/agent-wrapper.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    create 640 agent agent
}
```

Validate with `sudo logrotate -d /etc/logrotate.d/agent-wrapper`. Not required for initial proof; required for long-running appliance.

### 6. Point `.env` at wrapper

In `/home/voice/.env`:

```bash
AGENT_USER=agent
AGENT_BIN=/home/agent/artifacts/cursor-agent-wrapper
AGENT_WORKSPACE=/home/agent/personal
```

---

## Verify

```bash
# Artifacts dir and wrapper on disk, owned by agent
sudo -u agent test -x /home/agent/artifacts/cursor-agent-wrapper && echo ok
sudo -u agent test -d /home/agent/artifacts && echo ok

# voice may invoke wrapper as agent without password
sudo -u voice -H sudo -n -u agent -H -- /home/agent/artifacts/cursor-agent-wrapper --help

# Installer idempotent (voice deposits to agent)
sudo -u voice -H sudo -n /home/voice/raspberry-ai/agent-artifacts/scripts/install-cursor-agent-wrapper.sh
echo exit:$?

# Log appendable as agent, mode 640
sudo -u agent bash -c 'echo verify >> /var/log/agent-wrapper.log'
stat -c '%a %U %G' /var/log/agent-wrapper.log
# Expected: 640 agent agent
```

### Smoke test (wrapped agent, no `master.py`)

From the repo root as `voice`, with `.env` loaded (same `AGENT_*` as production):

```bash
cd /home/voice/raspberry-ai
set -a && . /home/voice/.env && set +a
python3 agent-artifacts/scripts/smoke-wrapped-agent.py "Say hello in five words."
```

This mirrors `CursorAgentSession` spawn (`sudo -u agent` when `AGENT_USER` is set, `start_new_session=True`, transcript on stdin only). **Stdout** is raw stream-json lines; **stderr** is the CLI (and optional wrapper log fallback). Use `--env-file /home/voice/.env` instead of `set -a` if you prefer.

**Result (2026-04-06):** Smoke test **passed**. stream-json events received (system → user → assistant deltas → result), exit code 0, wrapper log shows `start` → `spawn_real` → `exit child_exit=0`. The initial wrapper implementation used bare `setsid ... &` which broke stdin (bash redirects backgrounded jobs to `/dev/null`). Fixed by saving stdin to fd 3 before backgrounding and re-attaching it to the `setsid` child (`exec 3<&0; setsid -- "$REAL_BIN" "$@" <&3 &`). PGID isolation confirmed: child PGID equals child PID, differs from wrapper PGID.

---

## Provisioning order note

**Step 4b** (after `agent-user-setup.md`): the real Cursor CLI must exist under `agent` before the wrapper can spawn it. See `profiling-pi/AGENTS.md`.
