# Real-Time Scheduling Permissions

**Purpose:** Grant the `voice` user (via the `audio` group) real-time scheduling
and nice privileges so the recorder child can set SCHED_FIFO priority 50 and
fall back to `nice(-10)` without running as root.

---

## Target State

- `/etc/security/limits.d/99-realtime.conf` exists with three entries granting `@audio` rtprio 99, nice -20, and unlimited memlock
- `voice` is in the `audio` group (provisioned by `voice-user-setup.md`)
- After login, `ulimit -r` returns 99 and `ulimit -e` returns 40 for `voice`
- `chrt -f 50 sleep 1` succeeds as `voice` without permission errors

---

## Instructions

### 1. Confirm `voice` is in the `audio` group

```bash
groups voice | grep -q audio && echo "In audio group" || echo "Run: sudo usermod -aG audio voice"
```

### 2. Create the PAM limits drop-in

```bash
cat <<'EOF' | sudo tee /etc/security/limits.d/99-realtime.conf
@audio   -   rtprio     99
@audio   -   nice      -20
@audio   -   memlock   unlimited
EOF
```

### 3. Reboot

Limits only apply to new login sessions.

```bash
sudo reboot
```

### 4. (Optional) Remove RT throttling

Only needed if SCHED_FIFO tasks are being throttled despite correct limits:

```bash
echo "kernel.sched_rt_runtime_us = -1" | sudo tee /etc/sysctl.d/99-realtime.conf
sudo sysctl -p /etc/sysctl.d/99-realtime.conf
```

---

## Verify

Run as `voice` after reboot:

```bash
# 1. ulimits
ulimit -r
# Expected: 99

ulimit -e
# Expected: 40 (allows nice -20 to +19)

# 2. SCHED_FIFO at priority 50
chrt -f 50 sleep 1 && echo "SCHED_FIFO 50 OK"
# Expected: SCHED_FIFO 50 OK

# 3. nice fallback
renice -n -10 -p $$ && echo "nice(-10) fallback OK"
# Expected: nice(-10) fallback OK
```

---

## Notes

- On stock Raspberry Pi OS (Debian), regular users are blocked from real-time
  scheduling. The recorder child (`recorder_child_entry`) tries `SCHED_FIFO 50`
  on core 0 and falls back to `nice(-10)` — both require PAM limits to succeed
  without root.
- Setting privileges via `/etc/security/limits.d/` is the standard approach.
  It survives reboots and avoids running the voice agent as root or via sudo.
- Last validated: April 2026 on Raspberry Pi 4 with current Raspberry Pi OS (Trixie).
