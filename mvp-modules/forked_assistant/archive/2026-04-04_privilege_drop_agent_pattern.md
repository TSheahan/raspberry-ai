# Design Pattern Summary: Privileged Parent → Unprivileged Agent Child

**Purpose**  
This document provides a concise, self-contained reference for the chosen privilege-separation design pattern. It complements the proof-of-concept Python script (the version using `subprocess.Popen` with `user=`/`group=` followed by a parent privilege drop). An implementing agent (developer, CI/CD pipeline, or orchestration system) can use this as the authoritative blueprint.

## 1. Core Problem Being Solved
- The **parent process** must start with elevated privileges (root via `sudo`) to be able to launch children under arbitrary user accounts.
- Each **child process** (agent wrapper) must run under its own dedicated, unprivileged Linux user (`agent-user-42` in the example).
- After the child is successfully launched, the **parent must voluntarily drop its own root privileges** so that it cannot perform any further privileged operations.
- No `sudo` commands are used inside the code; the only `sudo` is the initial launch of the parent script.

## 2. High-Level Design Pattern (Step-by-Step Flow)
1. **Launch**  
   The parent script is started with `sudo python your_script.py` (or via a systemd service with `User=root`).

2. **Parent is root**  
   The process has effective UID 0.

3. **Spawn the agent child**  
   Use `subprocess.Popen(..., user=agent_username, group=agent_gid, ...)`  
   → The kernel performs the `setgid`/`setuid` drop *inside* the child before any Python code in the child runs.

4. **Parent drops its own privileges**  
   Immediately after the child is started, the parent calls `os.setgid()` + `os.setuid()` to become a normal user (usually `$SUDO_USER` or a fixed unprivileged account).

5. **Both processes now run unprivileged**  
   - Child = dedicated agent user (isolated, minimal permissions).  
   - Parent = normal user (cannot escalate again).

## 3. Why This Pattern?
- **Security** – Least-privilege principle: neither process retains root after startup.
- **Simplicity** – No `sudo` inside the code, no `setuid` binaries, no complex capability management.
- **Reliability** – The `user=`/`group=` parameters in `subprocess` (Python 3.8+) handle the delicate gid-before-uid ordering correctly.
- **Auditability** – The only privileged moment is the initial `sudo` launch, which is easy to log and control via sudoers or systemd.

## 4. Key Implementation Requirements (must follow)
- The script **must** check `os.getuid() == 0` at startup and exit with a clear error otherwise.
- Always drop the child’s privileges *via* the `user=`/`group=` arguments (never rely on the child to drop them itself).
- Parent drop happens **after** `Popen` returns (child is already launched).
- Target agent user must be a pre-created system user (`useradd -r -s /bin/false agent-user-42`).
- Use `start_new_session=True` on the child for clean process-group isolation.

## 5. Security Guarantees Provided
- Child never sees root credentials.
- Parent cannot regain root after dropping.
- No residual capabilities left (standard `setuid` semantics).
- Works even if the original caller was a sudoer; the parent ends up as that same user.

## 6. Usage / Deployment Notes
```bash
# One-time launch (for testing or simple daemons)
sudo python3 your_agent_launcher.py

# Production recommendation
# → Install as a systemd service with User=root, then ExecStart=/usr/bin/python3 /path/to/script.py
```

**Next steps for the implementing agent**  
1. Copy the POC script verbatim as the starting point.  
2. Replace the placeholder `agent_cmd` and `agent_username` with your actual values.  
3. Add any parent-side monitoring loop or shutdown handling you need.  
4. Test the privilege drop by checking `ps -eo user,pid,comm | grep your_agent` after launch.

This pattern is now the canonical design for the project. Any future changes (e.g., raw `os.fork()` variant or systemd integration) should be documented as deviations from this baseline.