"""
Smoke test: seeed-2mic-voicecard APA102 RGB LEDs.

Drives the 3 onboard APA102 LEDs over SPI to confirm the interface works.
Sequence: red → green → blue → white → cycle → off.

Run:
    cd ~/raspberry-ai
    source ~/venv/bin/activate
    python mvp-modules/2-mic/smoke_leds.py

Requires: spidev (pip install spidev)
Hardware:  SPI enabled (raspi-config), /dev/spidev0.1 present.
"""

import time
import spidev

NUM_LEDS = 3
SPI_BUS = 0
SPI_DEV = 1
SPI_SPEED = 8_000_000
BRIGHTNESS = 8  # 0-31; keep low — these are bright at close range


def _frame(pixels: list[tuple[int, int, int]], brightness: int = BRIGHTNESS) -> list[int]:
    """Build a complete APA102 frame for all LEDs."""
    start = [0x00] * 4
    led_frames = []
    for r, g, b in pixels:
        led_frames += [0xE0 | (brightness & 0x1F), b, g, r]
    end = [0xFF] * 4
    return start + led_frames + end


def write(spi, pixels: list[tuple[int, int, int]], brightness: int = BRIGHTNESS) -> None:
    data = _frame(pixels, brightness)
    spi.xfer2(data)


def all_same(spi, r: int, g: int, b: int, brightness: int = BRIGHTNESS) -> None:
    write(spi, [(r, g, b)] * NUM_LEDS, brightness)


def off(spi) -> None:
    all_same(spi, 0, 0, 0, 0)


def main() -> None:
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEV)
    spi.max_speed_hz = SPI_SPEED

    try:
        print("[LED] Red...")
        all_same(spi, 255, 0, 0)
        time.sleep(1.0)

        print("[LED] Green...")
        all_same(spi, 0, 255, 0)
        time.sleep(1.0)

        print("[LED] Blue...")
        all_same(spi, 0, 0, 255)
        time.sleep(1.0)

        print("[LED] White...")
        all_same(spi, 255, 255, 255)
        time.sleep(1.0)

        print("[LED] Cycle (one LED at a time)...")
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        for _ in range(3):
            for offset in range(NUM_LEDS):
                pixels = [(0, 0, 0)] * NUM_LEDS
                for j, c in enumerate(colors):
                    pixels[(j + offset) % NUM_LEDS] = c
                write(spi, pixels)
                time.sleep(0.3)

        print("[LED] Off.")
        off(spi)

    except KeyboardInterrupt:
        off(spi)
        print("\n[LED] Interrupted, LEDs off.")
    finally:
        spi.close()

    print("[DONE] LED smoke test complete.")


if __name__ == "__main__":
    main()
