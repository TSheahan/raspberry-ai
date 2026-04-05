# Agent User Setup

**Purpose:** Configure a dedicated `agent` Linux user to run the Cursor CLI subprocess in isolation. The voice assistant master process (`master.py`) runs as `voice`; the Cursor CLI agent runs as `agent` via a narrow sudoers entry. See `mvp-modules/forked_assistant/archive/2026-04-04_privilege_separation_analysis.md` for the full design rationale.

**Prerequisite:** `voice-user-setup.md` must be completed first.

---

## Prerequisites

- `voice` user exists and is provisioned (see `voice-user-setup.md`)
- A privileged user with `sudo` access is available for provisioning (e.g. `pi`)
- Run all provisioning commands below as the privileged user via `sudo`
- `voice` itself has **no** broad sudo — only the narrow NOPASSWD entry created in step 5

---

## Target State

- `agent` Linux user exists as a system user with home at `/home/agent`
- Cursor CLI installed at `/home/agent/.local/bin/agent`, authenticated
- `gh` authenticated for `agent` (HTTPS, GitHub.com)
- Repository cloned at `/home/agent/personal` (checkout of `main`)
- Sudoers entry: `voice ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent`
- SSH remote login denied for `voice` and `agent`
- `voice` can invoke the agent binary as `agent` from a non-TTY context without a password prompt
- `.env` at `/home/voice/.env` contains `AGENT_USER`, `AGENT_BIN`, `AGENT_WORKSPACE`

---

## Instructions

### 1. Create the `agent` Linux User

```bash
sudo useradd -r -m -d /home/agent -s /bin/bash agent
```

- `-r` — system user (no aging, below UID_MIN) [^1]
- `-m` — create home directory
- `-d /home/agent` — explicit home
- `-s /bin/bash` — interactive shell for maintenance via `sudo -iu agent`

Remote login is blocked at the SSH layer (see step 5a), not via nologin.

---

### 2. Install the Cursor CLI for `agent`

`agent` owns the Cursor CLI install end-to-end. Neither `voice` nor the profiling user needs this binary — the profiling user may already have its own Cursor CLI install available for profiling tasks.

```bash
sudo -u agent -H bash -c 'curl https://cursor.com/install -fsS | bash'
```

This runs the official installer as `agent`, placing the binary in `~agent/.local/bin/agent` and credentials in `~agent/.cursor/`.

---

### 3. Authenticate the Cursor CLI as `agent`

Authentication is handled during installation (step 2) if the installer prompts
for login. If not, authenticate separately:

```bash
sudo -u agent -H /home/agent/.local/bin/agent login
```

**Contingency:** the profiling user (or root) can access the binary at `/home/agent/.local/bin/agent` directly and re-authenticate if needed. `voice` does not need access to it for normal operation.

---

### 4. Set Up the Agent Workspace

The agent operates on a checkout of `main` from `https://github.com/TSheahan/personal`. Authentication is via the GitHub CLI device flow — no password, no SSH key required.

### 4a. Install and authenticate `gh` as `agent`

```bash
sudo apt install gh

# Authenticate as agent — prints a device code; open github.com/login/device
# on any browser and enter it
sudo -u agent -H gh auth login
```

At the prompts select: **GitHub.com → HTTPS → Login with a web browser**.

### 4b. Register `gh` as the Git credential helper and clone

```bash
sudo -u agent -H gh auth setup-git
sudo -u agent -H git clone https://github.com/TSheahan/personal /home/agent/personal
```

`gh auth setup-git` persists the credential helper so subsequent `git pull` calls inside the checkout also authenticate without prompting.

---

### 5. Add the Sudoers Entry

```bash
echo 'voice ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent' | \
  sudo tee /etc/sudoers.d/voice-assistant-agent
sudo chmod 440 /etc/sudoers.d/voice-assistant-agent
```

---

### 5a. Block Remote Login for Appliance Users

Both `voice` and `agent` keep `/bin/bash` for local maintenance (`sudo -iu voice`, `sudo -iu agent`). Remote login is blocked at the SSH layer:

```bash
echo 'DenyUsers voice agent' | \
  sudo tee /etc/ssh/sshd_config.d/deny-appliance-users.conf
sudo chmod 644 /etc/ssh/sshd_config.d/deny-appliance-users.conf
sudo sshd -t && sudo systemctl reload sshd
```

---

### 6. Configure `.env`

Add to `/home/voice/.env` (for `load_dotenv` to find):

```
AGENT_USER=agent
AGENT_BIN=/home/agent/.local/bin/agent
AGENT_WORKSPACE=/home/agent/personal
```

---

## Verify

```bash
# 1. agent user exists
id agent
# Expected: uid=...(agent) gid=...(agent) groups=...(agent),...

# 2. Cursor CLI installed and responds
sudo -u agent -H /home/agent/.local/bin/agent --version
# Expected: version string

# 3. Cursor CLI authenticated
sudo -u agent -H /home/agent/.local/bin/agent status
# Expected: shows authenticated account and subscription

# 4. gh authenticated
sudo -u agent -H gh auth status
# Expected: Logged in to github.com account ...

# 5. Workspace cloned
sudo -u agent -H git -C /home/agent/personal status
# Expected: On branch main, clean working tree

# 6. Sudoers entry valid
sudo visudo -c -f /etc/sudoers.d/voice-assistant-agent
# Expected: parsed OK

# 7. SSH login denied
ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no \
  voice@localhost echo "should fail" 2>&1
# Expected: Permission denied

ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no \
  agent@localhost echo "should fail" 2>&1
# Expected: Permission denied

# 8. Full invocation path (non-TTY, matching Popen)
sudo -u voice sudo -u agent -H -- /home/agent/.local/bin/agent --version < /dev/null
# Expected: version string without password prompt
```

### Post-Launch Verification

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

[^1]: The current `agent` on morpheus has UID 1001 (created before this profile existed). Cosmetic only — no runtime impact.
