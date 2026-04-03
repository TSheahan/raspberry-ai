# Raspberry Pi Real-Time Scheduling Setup  

**(for `recorder_child_entry` — SCHED_FIFO + nice fallback)**

### Why we did this (Reasoning)
- The recorder tries to pin to core 0 and set **SCHED_FIFO priority 50** for low-latency / real-time performance (critical for audio/recording stability).
- On stock Raspberry Pi OS (Debian), regular users are blocked from real-time scheduling → “Permission denied”.
- The code gracefully falls back to `nice(-10)`, but that can also fail without the right limits.
- **Best practice**: Give the user (via the `audio` group) the required privileges through PAM limits instead of running as root or sudo every time.
- This survives reboots and is the clean, intended way the recorder expects.

### One-time Setup (run once after fresh Pi image or rebuild)

```bash
# 1. Confirm you are in the audio group (most Pi users already are)
groups | grep -q audio && echo "✓ In audio group" || echo "Add yourself first: sudo usermod -aG audio $USER"

# 2. Create the limits drop-in file
sudo nano /etc/security/limits.d/99-realtime.conf
```

**Paste exactly this into the file:**
```
@audio   -   rtprio     99
@audio   -   nice      -20
@audio   -   memlock   unlimited
```

Save & exit (`Ctrl`+`O` → `Enter` → `Ctrl`+`X`).

```bash
# 3. Reboot (mandatory — limits only apply to new login sessions)
sudo reboot
```

### Verification (run after reboot)

```bash
ulimit -r          # should show 99 (or higher)
ulimit -e          # should show 40 (or higher) → allows nice(-20) to +19
```

**Real test commands** (must both succeed):
```bash
chrt -f 50 sleep 1 && echo "✅ SCHED_FIFO 50 OK"
renice -n -10 -p $$ && echo "✅ nice(-10) fallback OK"
```

If both green ✅ lines appear → you’re good. The recorder will now be able to set SCHED_FIFO 50 (or fall back silently).

### Optional (if you still see throttling issues)
```bash
echo "kernel.sched_rt_runtime_us = -1" | sudo tee /etc/sysctl.d/99-realtime.conf
sudo sysctl -p /etc/sysctl.d/99-realtime.conf
```

### Quick Re-apply Checklist (for future Pi rebuilds)
1. `sudo usermod -aG audio $USER` (if needed)
2. Create `/etc/security/limits.d/99-realtime.conf` with the three lines above
3. Reboot
4. Run the four verification commands

Done. No more permission-denied warnings for the recorder.  
*(Last applied: April 2026 — works on Raspberry Pi 4/5 with current Raspberry Pi OS)*