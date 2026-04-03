#!/usr/bin/env python3
"""
kernel-monitor: continuous dmesg surveillance for I2S/audio driver alarms.

Follows dmesg output in real time. On an alarm, writes every subsequent line
to a dated evidence file (prefixed with pre-alarm context) until the stream
goes quiet for EVIDENCE_CLOSE_SECS.  Every matched alarm line is also
appended to a persistent alarm log for longitudinal inspection.

Deployment: run as a systemd service (see kernel-monitor.service).
Log directory: /var/log/kernel-monitor/
Evidence files: /var/log/kernel-monitor/evidence/<timestamp>_event.txt
"""

import os
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_DIR       = Path("/var/log/kernel-monitor")
EVIDENCE_DIR  = LOG_DIR / "evidence"
ALARM_LOG     = LOG_DIR / "alarms.log"

# Lines of pre-alarm context written at the top of each evidence file.
CONTEXT_LINES = 40

# Seconds of silence after the last alarm line before closing the evidence
# file.  The confirmed crash gap between first BUG and fatal Oops is ~37s,
# so 60s ensures a single file covers the full event even at that cadence.
EVIDENCE_CLOSE_SECS = 60

# ---------------------------------------------------------------------------
# Pattern lists — defined together so the full filter surface is visible
# in one place.
# ---------------------------------------------------------------------------

# Alarm patterns — kernel-generated lines that indicate driver distress.
# Focused on kernel BUG/Oops/panic machinery and the I2S audio fault path.
_ALARM_SPECS = [
    # Kernel BUG macro — the primary indicator for the ac108 atomic-sleep bug.
    r"BUG:",
    # Explicit text emitted by __schedule_bug, always accompanies BUG: above.
    r"scheduling while atomic",
    # BCM I2S controller sync loss — observed immediately before first BUG:.
    r"I2S SYNC error",
    # Fatal memory fault — the crash mechanism after BUG accumulation.
    r"Unable to handle kernel",
    # Kernel oops header.
    r"Internal error: Oops",
    # Kernel panic — last line before reboot.
    r"Kernel panic",
    # Stack trace header — emitted by dump_stack after every BUG/Oops.
    r"Call trace:",
    # OOM killer — unrelated crash vector, worth capturing.
    r"oom.killer",
    r"Out of memory",
]

# Noise rejection — lines that are suppressed from evidence and alarm log.
#
# "bcm" in the device name does NOT mean audio on the Pi 4.  The audio path
# is:  bcm2835-i2s (I2S controller, relevant) → ac10x-codec / seeed-voicecard
# The bcm* names below are networking, camera, BT, and video subsystems.
_NOISE_SPECS = [
    r"bcmgenet",           # Ethernet (RGMII MAC) — link events, MDIO polls
    r"brcmfmac",           # WiFi (SDIO) — association, power-save chatter
    r"hci_uart_bcm",       # Bluetooth UART transport
    r"Bluetooth:",         # BT subsystem messages
    r"bcm2835.isp",        # Camera ISP
    r"bcm2835.codec",      # Video codec
    r"bcm2835.v4l2",       # V4L2 camera driver
    r"bcm2835.mmal",       # Multimedia Abstraction Layer (camera)
    r"v3d ",               # GPU (space avoids matching "v3d_" in stack traces)
    r"brcm.pcie",          # PCIe bridge
    # Boot-time overlay warnings — not runtime faults.
    r"OF: overlay: WARNING: memory leak",
    # Staging-module quality warnings — always present, not actionable.
    r"module is from the staging directory",
    # Regulator dummy-stub messages on every boot.
    r"supply \w+ not found, using dummy regulator",
    # Sound card name truncation — cosmetic, boot-time only.
    r"driver name too long",
]

ALARM_RES = [re.compile(p) for p in _ALARM_SPECS]
NOISE_RES  = [re.compile(p) for p in _NOISE_SPECS]

# ---------------------------------------------------------------------------
# Verbose mode — set KM_VERBOSE=1 (the default) to also log noise-matched
# lines tagged [V].  Gives immediate observable output on first runs since
# BCM networking events are frequent, confirming the pipeline is working
# end-to-end before any real audio alarm has been triggered.
# Disable with KM_VERBOSE=0 once exercised.
# ---------------------------------------------------------------------------

VERBOSE = os.getenv("KM_VERBOSE", "1") == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_noise(line: str) -> bool:
    return any(p.search(line) for p in NOISE_RES)


def is_alarm(line: str) -> bool:
    return any(p.search(line) for p in ALARM_RES)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def log(msg: str) -> None:
    print(f"[kernel-monitor] {utc_now()}  {msg}", flush=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # Rolling window of recent lines for pre-alarm context.
    context: deque[str] = deque(maxlen=CONTEXT_LINES)

    evidence_file   = None
    last_alarm_mono = 0.0
    event_count     = 0

    log(
        f"starting — log_dir={LOG_DIR}  context={CONTEXT_LINES}"
        f"  close_secs={EVIDENCE_CLOSE_SECS}  verbose={VERBOSE}"
    )

    with open(ALARM_LOG, "a", buffering=1) as alarm_log:

        proc = subprocess.Popen(
            ["dmesg", "--follow", "--time-format=iso"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,          # line-buffered on the Python side
        )

        try:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                mono = time.monotonic()

                # --- Close stale evidence file after quiet period -----------
                if evidence_file is not None:
                    if (mono - last_alarm_mono) > EVIDENCE_CLOSE_SECS:
                        evidence_file.write(
                            f"# --- evidence closed  {utc_now()} ---\n"
                        )
                        evidence_file.close()
                        evidence_file = None
                        log(f"evidence file closed (event {event_count})")

                # --- Noise gate ---------------------------------------------
                if is_noise(line):
                    if VERBOSE:
                        # Log noise hits tagged [V] so first runs produce
                        # immediate output confirming the pipeline is live.
                        # BCM networking/BT events are frequent enough that
                        # a match should appear within seconds of startup.
                        alarm_log.write(f"{utc_now()}  [V] {line}\n")
                        print(f"[V] {line}", flush=True)
                    context.append(line)
                    continue

                alarming = is_alarm(line)

                # --- Alarm handling -----------------------------------------
                if alarming:
                    last_alarm_mono = mono
                    alarm_log.write(f"{utc_now()}  [A] {line}\n")
                    print(f"[A] {line}", flush=True)

                    if evidence_file is None:
                        event_count += 1
                        path = EVIDENCE_DIR / f"{file_tag()}_event{event_count:03d}.txt"
                        evidence_file = open(path, "w", buffering=1)
                        evidence_file.write(
                            f"# kernel-monitor evidence — event {event_count} — {utc_now()}\n"
                        )
                        evidence_file.write(
                            f"# --- pre-alarm context ({len(context)} lines) ---\n"
                        )
                        for ctx_line in context:
                            evidence_file.write(ctx_line + "\n")
                        evidence_file.write("# --- alarm ---\n")
                        log(f"alarm: opening evidence file {path.name}")

                # --- Write every non-noise line while evidence is open ------
                # This captures stack frames, register dumps, module lists,
                # and the full Oops/panic body — none of which individually
                # match alarm patterns but are essential for diagnosis.
                if evidence_file is not None:
                    evidence_file.write(line + "\n")

                context.append(line)

        except KeyboardInterrupt:
            pass
        finally:
            if evidence_file is not None:
                evidence_file.write(f"# --- evidence closed (shutdown) {utc_now()} ---\n")
                evidence_file.close()
            proc.terminate()
            log("stopped")


if __name__ == "__main__":
    main()
