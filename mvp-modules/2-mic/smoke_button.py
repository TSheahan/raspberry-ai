"""
Smoke test: seeed-2mic-voicecard user button (BCM GPIO17, active-low).

Polls the pin with debounce and prints PRESSED / RELEASED on transitions.
Ctrl+C exits cleanly.

Run:
    cd ~/raspberry-ai
    source ~/venv/bin/activate
    pip install RPi.GPIO   # once per venv (not pulled in by other 2-mic smokes)
    python mvp-modules/2-mic/smoke_button.py

Hardware:  Seeed ReSpeaker 2-Mics Pi HAT — user button on GPIO17 (pinout.xyz,
           Seeed wiki).

Requires:  RPi.GPIO; `voice` in the `gpio` group for /dev/gpiomem (see
           profiling-pi/voice-user-setup.md), or run with sudo.
"""

from __future__ import annotations

import sys
import time

try:
    import RPi.GPIO as GPIO
except ImportError:
    print("[ERR]  Missing RPi.GPIO — install with:  pip install RPi.GPIO", file=sys.stderr)
    raise SystemExit(1) from None

BUTTON_BCM = 17
POLL_S = 0.02
DEBOUNCE_S = 0.04


def main() -> None:
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_BCM, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    print(
        f"[BTN]  Monitoring BCM GPIO {BUTTON_BCM} (HAT user button, active-low).\n"
        "[BTN]  Press and release the button — Ctrl+C to exit.\n",
        flush=True,
    )

    prev = GPIO.input(BUTTON_BCM)

    try:
        while True:
            time.sleep(POLL_S)
            raw = GPIO.input(BUTTON_BCM)
            if raw == prev:
                continue
            time.sleep(DEBOUNCE_S)
            stable = GPIO.input(BUTTON_BCM)
            if stable != raw:
                continue
            if stable == prev:
                continue
            prev = stable
            if stable == GPIO.LOW:
                print("[BTN]  PRESSED  (contact closed, line pulled low)", flush=True)
            else:
                print("[BTN]  RELEASED (line high, internal pull-up)", flush=True)
    except KeyboardInterrupt:
        print("\n[BTN]  Exiting.", flush=True)
    finally:
        GPIO.cleanup()

    print("[DONE] Button smoke test complete.")


if __name__ == "__main__":
    main()
