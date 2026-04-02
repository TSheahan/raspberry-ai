"""
P-1: ReSpeaker Channel Probe + Channel Provenance + Mixer State

Three probes, run in sequence:

1. Channel config probe (original P-1): confirms 1-ch at 16kHz delivers real
   audio; 2-ch/4-ch produce silence due to S16_LE format mismatch with AC108.

2. Tap test: records 10 seconds of 1-ch audio with windowed RMS display.
   Tap each of the 4 mic positions in turn (~2s each). If only one position
   produces a peak, 1-ch = channel 0 (single mic). If all four produce peaks,
   the driver is hardware-mixing all mics before presenting to ALSA.

3. Mixer state: dumps ALSA mixer controls and /etc/asound.conf routing config.
   Surfaces PGA gain settings and whether a ttable (explicit channel routing)
   is in use. This is the input for any quality uplift via gain or mix changes.

Run on Pi (from forked_assistant/ directory):
    python test/smoke_respeaker_channels.py

No inference. Hardware probe only.
"""

import math
import os
import struct
import subprocess
import time

import pyaudio

DEVICE_INDEX    = 1
SAMPLE_RATE     = 16_000
FORMAT          = pyaudio.paInt16
SAMPLE_WIDTH    = 2          # bytes per sample (int16)
CHUNK_FRAMES    = 512        # audio frames per read call (~32 ms at 16 kHz mono)
CHANNEL_CONFIGS = [1, 2, 4]

TAP_SECONDS     = 10         # tap test recording duration
TAP_WINDOW      = 800        # RMS window size in frames (~50 ms at 16 kHz)
RMS_BAR_WIDTH   = 36


# ---------------------------------------------------------------------------
# Probe 1: device info
# ---------------------------------------------------------------------------

def print_device_info(pa: pyaudio.PyAudio) -> None:
    info     = pa.get_device_info_by_index(DEVICE_INDEX)
    host_api = pa.get_host_api_info_by_index(info["hostApi"])["name"]
    print(f"[P-1] Device index        : {DEVICE_INDEX}")
    print(f"[P-1] Name                : {info['name']}")
    print(f"[P-1] Max input channels  : {info['maxInputChannels']}")
    print(f"[P-1] Default sample rate : {info['defaultSampleRate']}")
    print(f"[P-1] Host API            : {host_api}")
    print()


# ---------------------------------------------------------------------------
# Probe 1: per-channel-count open + sample check
# ---------------------------------------------------------------------------

def probe_channels(pa: pyaudio.PyAudio, channels: int) -> None:
    label = f"[P-1] {channels}ch"

    try:
        stream = pa.open(
            format=FORMAT,
            channels=channels,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=DEVICE_INDEX,
            frames_per_buffer=CHUNK_FRAMES,
        )
    except Exception as exc:
        print(f"{label} OPEN FAILED : {exc}")
        return

    try:
        raw = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
    except Exception as exc:
        print(f"{label} READ FAILED : {exc}")
        return
    finally:
        stream.stop_stream()
        stream.close()

    n_bytes   = len(raw)
    n_samples = n_bytes // SAMPLE_WIDTH

    # First 8 int16 values reveal interleaving:
    #   1-ch → [s0, s1, s2, s3, s4, s5, s6, s7]
    #   2-ch → [L0, R0, L1, R1, L2, R2, L3, R3]
    #   4-ch → [c0_s0, c1_s0, c2_s0, c3_s0, c0_s1, c1_s1, c2_s1, c3_s1]
    n_print = min(8, n_samples)
    first8  = list(struct.unpack_from(f"<{n_print}h", raw))

    print(f"{label} OPEN OK")
    print(f"{label} bytes_read={n_bytes}  n_samples={n_samples}  "
          f"(expected {CHUNK_FRAMES * channels * SAMPLE_WIDTH} bytes for {CHUNK_FRAMES} frames × {channels}ch)")
    print(f"{label} first {n_print} int16 samples : {first8}")
    print()


# ---------------------------------------------------------------------------
# Probe 2: tap test — channel provenance
# ---------------------------------------------------------------------------

def _rms_bar(value: float, peak: float) -> str:
    filled = int(RMS_BAR_WIDTH * min(value / peak, 1.0)) if peak > 0 else 0
    return "[" + "#" * filled + "." * (RMS_BAR_WIDTH - filled) + "]"


def tap_test(pa: pyaudio.PyAudio) -> None:
    print("=" * 60)
    print("PROBE 2 — Tap Test: channel provenance")
    print("=" * 60)
    print("[TAP] Determines if 1-ch is a single mic or a hardware mix.")
    print("[TAP] Will record 10 seconds. Tap each of the 4 mic positions")
    print("[TAP] in turn (~2 seconds each), then stop.")
    print()

    for i in range(3, 0, -1):
        print(f"[TAP] Starting in {i}...")
        time.sleep(1.0)

    stream = pa.open(
        format=FORMAT,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=DEVICE_INDEX,
        frames_per_buffer=TAP_WINDOW,
    )

    windows: list[tuple[float, float]] = []   # (timestamp, rms)
    n_windows = (TAP_SECONDS * SAMPLE_RATE) // TAP_WINDOW

    print("[TAP] Recording — tap each mic position now...")
    for i in range(n_windows):
        raw     = stream.read(TAP_WINDOW, exception_on_overflow=False)
        samples = struct.unpack_from(f"<{TAP_WINDOW}h", raw)
        rms     = math.sqrt(sum(s * s for s in samples) / TAP_WINDOW)
        t       = i * TAP_WINDOW / SAMPLE_RATE
        windows.append((t, rms))

    stream.stop_stream()
    stream.close()

    peak = max(rms for _, rms in windows) or 1.0
    ms_per_window = TAP_WINDOW / SAMPLE_RATE * 1000

    print(f"\n[TAP] Results ({ms_per_window:.0f} ms per row, peak_rms={peak:.1f}):")
    for t, rms in windows:
        print(f"  {t:5.1f}s  {_rms_bar(rms, peak)}  {rms:7.1f}")

    print()
    print("[TAP] Interpret:")
    print("[TAP]   One position peaks, others silent → 1-ch = channel 0 (single mic)")
    print("[TAP]   All positions produce peaks       → driver hardware-mixes all 4 mics")
    print()


# ---------------------------------------------------------------------------
# Probe 3: ALSA mixer state — quality uplift surface
# ---------------------------------------------------------------------------

def print_mixer_state() -> None:
    print("=" * 60)
    print("PROBE 3 — ALSA Mixer State: quality uplift surface")
    print("=" * 60)

    # amixer for the seeed card (card 3 based on hw:3,0 from P-1)
    print("[MIX] amixer -c 3 (seeed4micvoicec controls):")
    result = subprocess.run(["amixer", "-c", "3"], capture_output=True, text=True)
    if result.returncode == 0:
        # Filter to input-relevant lines to keep output manageable
        relevant = [
            ln for ln in result.stdout.splitlines()
            if any(kw in ln for kw in
                   ("Capture", "Gain", "Volume", "Mic", "PGA", "ADC",
                    "Mix", "Route", "Input", "Switch", "values"))
        ]
        for ln in relevant:
            print(f"  {ln}")
    else:
        print(f"  FAILED: {result.stderr.strip()}")
    print()

    # /etc/asound.conf — look for ttable (explicit channel routing)
    asound_path = "/etc/asound.conf"
    print(f"[MIX] {asound_path}:")
    if os.path.exists(asound_path):
        with open(asound_path) as f:
            print(f.read())
    else:
        print(f"  Not found at {asound_path}")

    # ac108_asound.state — codec register snapshot restored on boot
    state_candidates = [
        "/etc/voicecard/ac108_asound.state",
        "/var/lib/alsa/asound.state",
        os.path.expanduser("~/seeed-voicecard/ac108_asound.state"),
    ]
    print("[MIX] AC108 state file (codec register snapshot):")
    for path in state_candidates:
        if os.path.exists(path):
            print(f"  Found: {path}")
            with open(path) as f:
                content = f.read()
            # Print lines containing mixing/routing keywords
            for ln in content.splitlines():
                if any(kw in ln for kw in
                       ("Gain", "Mix", "Route", "ADC", "PGA", "Input", "value")):
                    print(f"    {ln.strip()}")
            break
    else:
        print("  Not found at any candidate path")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    pa = pyaudio.PyAudio()
    try:
        print("=" * 60)
        print("PROBE 1 — Channel Config (P-1 original)")
        print("=" * 60)
        print_device_info(pa)
        for ch in CHANNEL_CONFIGS:
            probe_channels(pa, ch)

        # tap_test(pa)
    finally:
        pa.terminate()

    print_mixer_state()


if __name__ == "__main__":
    main()
