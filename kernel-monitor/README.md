# kernel-monitor

Continuous `dmesg` surveillance for I2S/audio driver alarms on Raspberry Pi 4.

## Background

The voice assistant runs a ReSpeaker 4-Mic HAT over I2S (AC108 codec, seeed-voicecard driver). A known bug in the AC108 driver calls `regmap_i2c_write` from an atomic/interrupt context — a sleeping operation performed while interrupts are disabled. The kernel detects this as `BUG: scheduling while atomic`, which cascades through an Oops to a kernel panic and hard reboot.

The confirmed crash sequence:

```
bcm2835-i2s fe203000.i2s: I2S SYNC error!        ← precursor
BUG: scheduling while atomic: python/PID/...      ← ac108_set_clock called I2C in atomic ctx
  Call trace: ac108_set_clock → bcm2835_i2c_xfer → wait_for_completion_timeout → schedule
                                                  ← ~37s gap (stream stop path)
BUG: scheduling while atomic: python/PID/...      ← same bug on TRIG_STOP
Unable to handle kernel paging request at ...     ← fatal memory fault
Internal error: Oops: ...
Kernel panic - not syncing: Aiee, killing interrupt handler!
```

`pstore`/ramoops (64 KB) fills before the panic completes, so crash evidence does **not**
survive reboot. In-flight capture by this monitor is the only reliable collection mechanism.

## What it does

- Follows `dmesg --follow` in real time as a systemd service
- Matches alarm lines (`[A]`) against kernel BUG/Oops/panic and I2S fault patterns
- Rejects BCM networking, camera, and Bluetooth noise (`[V]` in verbose mode)
- Appends every alarm line to a persistent log: `/var/log/kernel-monitor/alarms.log`
- On first alarm, opens a timestamped evidence file under `/var/log/kernel-monitor/evidence/`,
  prepending 40 lines of pre-alarm context
- Captures all subsequent non-noise lines for 60 seconds after the last alarm line,
  ensuring the full crash sequence (including the ~37s TRIG_START→TRIG_STOP gap) lands
  in one file

## Playbooks

### After a crash (Pi has rebooted)

```bash
# 1. Alarm log — fsynced after every [A] write, best chance of surviving the panic
sudo tail -50 /var/log/kernel-monitor/alarms.log

# 2. Evidence file — full crash sequence with 40-line pre-alarm context
sudo cat $(sudo ls -t /var/log/kernel-monitor/evidence/*.txt | head -1)

# 3. Previous boot's kernel ring buffer — journald is persistent on this Pi
sudo journalctl -k -b -1 --no-pager | grep -E "BUG|panic|Oops|I2S|ac10|seeed"
```

The `[A] I2S SYNC error` line in `alarms.log` is the earliest observable distress
signal — it is fsynced to disk before the first `BUG:` arrives ~1ms later.

### Watching live while the agent runs

```bash
sudo journalctl -u kernel-monitor -f
```

`[A]` lines indicate real distress. `[V]` lines are noise (suppress with `KM_VERBOSE=0`
once the pipeline has been exercised).

### Checking service health

```bash
sudo systemctl status kernel-monitor
sudo journalctl -u kernel-monitor --since "1 hour ago" --no-pager | grep '\[A\]'
```

### Updating after a code change

```bash
git pull
sudo systemctl restart kernel-monitor
sudo systemctl status kernel-monitor --no-pager
```

### Disabling verbose mode

Once the pipeline is confirmed working, silence `[V]` output. Edit the deployed unit:

```bash
sudo systemctl edit kernel-monitor
```

Add:
```ini
[Service]
Environment=KM_VERBOSE=0
```

Then `sudo systemctl restart kernel-monitor`. Or at the command line:
`sudo KM_VERBOSE=0 python3 monitor.py`.

---

## Install

```bash
sudo bash kernel-monitor/install.sh
```

The script:
1. Writes a systemd unit to `/etc/systemd/system/kernel-monitor.service` with `ExecStart`
   resolved to the script's location in the checked-out repo
2. Runs `systemctl daemon-reload && systemctl enable --now kernel-monitor`
3. Prints `systemctl status` output to confirm the service is live

## Uninstall

```bash
sudo bash kernel-monitor/uninstall.sh
```

Stops and disables the service, removes the unit file. Logs in `/var/log/kernel-monitor/`
are left in place — remove manually if not needed.

## Log layout

```
/var/log/kernel-monitor/
├── alarms.log                          ← every matched line, timestamped, tagged [A] or [V]
└── evidence/
    └── 2026-04-03T20-33-06Z_event001.txt  ← full event capture with pre-alarm context
```

### Reading alarms.log

```
2026-04-03T20:33:06+00:00  [A] 2026-04-04T00:44:05,... bcm2835-i2s fe203000.i2s: I2S SYNC error!
2026-04-03T20:33:07+00:00  [A] 2026-04-04T00:44:05,... BUG: scheduling while atomic: python/...
```

`[A]` = real alarm (BUG/Oops/I2S fault). `[V]` = verbose noise hit (BCM networking/BT), only
present when `KM_VERBOSE=1`.

### Evidence files

Each file begins with a header and the pre-alarm context window, followed by `# --- alarm ---`
and every line received (across all subsystems) until 60 seconds of silence.

## Configuration

All configuration is at the top of `monitor.py`:

| Variable | Default | Effect |
|---|---|---|
| `KM_VERBOSE` (env) | `1` | Log noise-matched lines as `[V]`. Set `0` in the service env to silence. |
| `CONTEXT_LINES` | `40` | Pre-alarm lines prepended to each evidence file. |
| `EVIDENCE_CLOSE_SECS` | `60` | Seconds of silence before closing an evidence file. |

## Alarm patterns

| Pattern | Rationale |
|---|---|
| `BUG:` | Kernel BUG macro — primary indicator of the ac108 atomic-sleep fault |
| `scheduling while atomic` | Text from `__schedule_bug`, always accompanies `BUG:` |
| `I2S SYNC error` | BCM I2S controller sync loss — precursor to crash |
| `Unable to handle kernel` | Fatal kernel memory fault |
| `Internal error: Oops` | Kernel oops header |
| `Kernel panic` | Final line before reboot |
| `Call trace:` | Stack trace header — everything after is captured in the evidence window |
| `oom.killer` / `Out of memory` | OOM kill events |

## Noise rejection

`bcm` in the kernel device name does **not** mean audio on Pi 4. The audio path is
`bcm2835-i2s → ac10x-codec → seeed-voicecard` (I2S + I2C). Everything below is
networking, camera, BT, or video — rejected as noise:

`bcmgenet` (Ethernet), `brcmfmac` (WiFi), `hci_uart_bcm` / `Bluetooth:` (BT),
`bcm2835-isp` / `bcm2835-codec` / `bcm2835-v4l2` / `bcm2835-mmal` (camera/video),
`v3d` (GPU), `brcm-pcie` (PCIe bridge), plus boot-time overlay and staging-module notices.

Note: `bcm2835-i2s` is **not** rejected — it is the I2S audio controller and emits
`I2S SYNC error` immediately before the crash.
