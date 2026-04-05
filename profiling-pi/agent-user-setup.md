# Agent User Setup

**Purpose:** Configure a dedicated `agent` Linux user to run the Cursor CLI subprocess in isolation. The voice assistant master process (`master.py`) runs as `voice`; the Cursor CLI agent runs as `agent` via a narrow sudoers entry. See `mvp-modules/forked_assistant/archive/2026-04-04_privilege_separation_analysis.md` for the full design rationale.

**Prerequisite:** `voice-user-setup.md` must be completed first.

---

## Prerequisites

- `voice` user exists and is provisioned (see `voice-user-setup.md`)
- `voice` has `sudo` access (for provisioning steps below)
- Run all commands below as `voice` via `sudo` where required

---

## 1. Create the `agent` Linux User

```bash
sudo useradd -r -m -d /home/agent -s /usr/sbin/nologin agent
```

- `-r` — system user (no aging, below UID_MIN)
- `-m` — create home directory
- `-d /home/agent` — explicit home
- `-s /usr/sbin/nologin` — no interactive login shell

---

## 2. Install the Cursor CLI for `agent`

`agent` owns the Cursor CLI install end-to-end. The `voice` account does not need the binary.

```bash
sudo -u agent -H bash -c 'curl https://cursor.com/install -fsS | bash'
```

This runs the official installer as `agent`, placing the binary in `~agent/.local/bin/agent` and credentials in `~agent/.cursor/`.

Verify:

```bash
sudo -u agent -H /home/agent/.local/bin/agent --version
```

---

## 3. Authenticate the Cursor CLI as `agent`

Authentication is handled during installation (step 2) if the installer prompts
for login. If not, authenticate separately:

```bash
# Temporarily allow a login shell for agent, authenticate, then restore
# TODO: next profiling can adopt shell lockout IF it won't break agentic operations on the repository
sudo usermod -s /bin/bash agent
sudo -u agent -H /home/agent/.local/bin/agent login
sudo usermod -s /usr/sbin/nologin agent
```

Verify:

```bash
sudo -u agent -H /home/agent/.local/bin/agent status
```

Expected: shows authenticated account and subscription.

**Contingency:** root can access the binary at `/home/agent/.local/bin/agent` directly and re-authenticate if needed. `voice` does not need access to it for normal operation.

---

## 4. Set Up the Agent Workspace

The agent operates on a checkout of `main` from `https://github.com/TSheahan/raspberry-ai`. Authentication is via the GitHub CLI device flow — no password, no SSH key required.

### 4a. Install and authenticate `gh` as `agent`

```bash
sudo apt install gh

# Authenticate as agent — prints a device code; open github.com/login/device
# on any browser and enter it
sudo usermod -s /bin/bash agent
sudo -u agent -H gh auth login
sudo usermod -s /usr/sbin/nologin agent
```

At the prompts select: **GitHub.com → HTTPS → Login with a web browser**.

Verify:

```bash
sudo -u agent -H gh auth status
```

### 4b. Register `gh` as the Git credential helper and clone

```bash
sudo -u agent -H gh auth setup-git
sudo -u agent -H git clone https://github.com/TSheahan/raspberry-ai /home/agent/raspberry-ai
```

`gh auth setup-git` persists the credential helper so subsequent `git pull` calls inside the checkout also authenticate without prompting.

---

## 5. Add the Sudoers Entry

```bash
echo 'voice ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent' | \
  sudo tee /etc/sudoers.d/voice-assistant-agent
sudo chmod 440 /etc/sudoers.d/voice-assistant-agent
```

Verify the file is valid:

```bash
sudo visudo -c -f /etc/sudoers.d/voice-assistant-agent
```

Expected: `...file parsed OK`

---

## 6. Verify the Full Invocation Path

Test that `voice` can run the agent binary as `agent` from a non-TTY context (matching the Popen execution environment):

```bash
sudo -u voice bash -c 'sudo -u agent -H /home/agent/.local/bin/agent --version < /dev/null'
```

Expected: prints version string without a password prompt.

---

## 7. Configure `.env`

Add to `/home/voice/.env` (for `load_dotenv` to find):

```
AGENT_USER=agent
AGENT_BIN=/home/agent/.local/bin/agent
AGENT_WORKSPACE=/home/agent/raspberry-ai
```

---

## 8. Post-Launch Verification

After starting the voice assistant and triggering a wake word:

```bash
ps -eo user,pid,ppid,comm | grep agent
```

Expected output (two lines):

```
agent  <pid>   <master-pid>  sudo
agent  <pid2>  <pid>         agent
```

If you see `voice` instead of `agent` in the first column, the `AGENT_USER` env var is not being picked up — check the `.env` file path and `load_dotenv` call.
