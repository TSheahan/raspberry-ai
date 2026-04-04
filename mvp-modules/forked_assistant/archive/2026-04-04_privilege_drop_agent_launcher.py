import os
import pwd
import subprocess
import sys

def get_original_user():
    """Return the user who ran 'sudo' (or fall back to current user)."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return pwd.getpwnam(sudo_user)
    # fallback if not started via sudo
    return pwd.getpwuid(os.getuid())

def drop_privileges(username: str):
    """Drop real + effective UID/GID. Must be called while still root."""
    pw = pwd.getpwnam(username)
    os.setgid(pw.pw_gid)      # gid FIRST
    os.setuid(pw.pw_uid)
    # Optional but nice for cleanliness
    os.environ["HOME"] = pw.pw_dir
    os.environ["USER"] = pw.pw_name
    os.environ["LOGNAME"] = pw.pw_name

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
if os.getuid() != 0:
    print("Error: This script must be started with sudo", file=sys.stderr)
    sys.exit(1)

agent_username = "agent-user-42"
parent_drop_to  = get_original_user().pw_name   # or hard-code "some-normal-user"

print(f"Starting as root (PID {os.getpid()})")

# 1. Spawn the agent child as the dedicated agent user
agent_cmd = ["/usr/bin/python3", "-m", "myagent.module"]
child = subprocess.Popen(
    agent_cmd,
    user=agent_username,          # child drops here
    group=pwd.getpwnam(agent_username).pw_gid,
    start_new_session=True,
)

print(f"Agent child started (PID {child.pid}) as user '{agent_username}'")

# 2. Parent now drops its own privileges
print(f"Parent dropping privileges to '{parent_drop_to}'")
drop_privileges(parent_drop_to)

print(f"Parent is now running as '{parent_drop_to}' (PID {os.getpid()})")

# Parent continues here with normal-user permissions
# e.g. monitoring loop, HTTP server, etc.
try:
    while True:
        # your parent logic here
        pass
except KeyboardInterrupt:
    print("Shutting down...")
    child.terminate()
    child.wait()