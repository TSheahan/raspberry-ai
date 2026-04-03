user brief for initial clean-up

kernel-monitor captures the need, and development, for better visibility over the health state of the raspberry pi running the voice agent.

information about failures seen is gettable from files under `mvp-modules`. looking at mentions of 'crash', 'buffer' etc.

dmesg presents a monitoring surface which is currently under-utilized.

a systemd service and a monitoring script could log, and perhaps retain problem evidence, when alarms emerge in dmesg.

'bcm' noise should be rejected.

---

## Known failure anatomy (2026-04-03 crash dmesg)

The confirmed crash sequence for the ac108 I2C-in-atomic bug is:

```
bcm2835-i2s fe203000.i2s: I2S SYNC error!          ← precursor; I2S clock lost sync
BUG: scheduling while atomic: python/PID/0x00000002 ← ac108_set_clock calls I2C inside spin_lock
  Call trace: ... ac108_set_clock → bcm2835_i2c_xfer → wait_for_completion_timeout → schedule
BUG: scheduling while atomic: python/PID/0x00000000 ← second BUG on the same trigger path
  Call trace: ... do_sys_poll → schedule_hrtimeout_range
                                                    ← ~37s gap (TRIG_STOP path)
BUG: scheduling while atomic: python/PID/0x00000002 ← ac108_set_clock called again on stream stop
  Call trace: ... ac108_set_clock → bcm2835_i2c_xfer → wait_for_completion_timeout → schedule
Unable to handle kernel paging request at virtual address 0000007f...
Internal error: Oops: 000000008200000b [#1] PREEMPT SMP
  (register dump, module list)
pstore: backend (ramoops) writing error (-28)       ← ramoops full; evidence NOT persisted
Kernel panic - not syncing: Aiee, killing interrupt handler!
```

Key facts:
- `bcm2835-i2s` IS audio hardware (I2S controller); its errors are alarms, not noise.
- "bcm noise" = `bcmgenet` (ethernet), `brcmfmac` (WiFi), `bcm2835-isp` (camera),
  `bcm2835-codec` (video), `hci_uart_bcm` (BT) — none are in the audio signal path.
- Driver ENTER/EXIT instrumentation visible in the 2026-04-03 dump has been stripped;
  the monitor must rely only on kernel-generated alarm messages.
- ramoops (64 KB) fills before the panic completes → crash evidence does NOT survive
  reboot. In-flight capture by the monitor is the only reliable collection mechanism.
- The ~37s gap between first and third BUG requires `EVIDENCE_CLOSE_SECS = 60` to
  coalesce the full crash into one evidence file.

## Solution

`monitor.py` — Python script that follows `dmesg --follow --time-format=iso` and:
- Matches alarm lines against kernel BUG/Oops/panic and I2S fault patterns.
- Rejects bcm networking/camera/BT noise.
- Appends every alarm line to `/var/log/kernel-monitor/alarms.log`.
- On first alarm, opens a timestamped evidence file under
  `/var/log/kernel-monitor/evidence/`, prepending the last 40 context lines.
- Captures all subsequent non-noise lines into the evidence file until 60s
  of silence, ensuring the full crash sequence lands in one file.

`kernel-monitor.service` — systemd unit. Install:
```
sudo cp kernel-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kernel-monitor
sudo systemctl status kernel-monitor
```

agentic loop to commence after folder cleanup/initialization:
- resolve the problem space
- describe the solution
- clarify any platform queries by read-only interrogation on the raspberry pi
- stub implementation to be produced locally
- loop stop criteria: above are satisfied or a significant need for user feedback emerges

finally a check-point for user feedback
