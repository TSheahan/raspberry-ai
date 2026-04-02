"""
P-1: ReSpeaker Channel Probe

Determines the actual channel count delivered by PyAudio for device index 1.
The ring buffer and all inference code assume 16 kHz int16 mono. If the
ReSpeaker presents as multi-channel, all downstream processing is silently wrong.

Run on Pi (from forked_assistant/ directory):
    python test/smoke_respeaker_channels.py

No inference. Hardware probe only.
"""

import struct
import pyaudio

DEVICE_INDEX    = 1
SAMPLE_RATE     = 16_000
FORMAT          = pyaudio.paInt16
SAMPLE_WIDTH    = 2          # bytes per sample (int16)
CHUNK_FRAMES    = 512        # audio frames per read call (~32 ms at 16 kHz mono)
CHANNEL_CONFIGS = [1, 2, 4]


# ---------------------------------------------------------------------------
# Device info
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
# Per-channel-count probe
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
        stream.stop_stream()
        stream.close()
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
    n_print  = min(8, n_samples)
    first8   = list(struct.unpack_from(f"<{n_print}h", raw))

    print(f"{label} OPEN OK")
    print(f"{label} bytes_read={n_bytes}  n_samples={n_samples}  "
          f"(expected {CHUNK_FRAMES * channels * SAMPLE_WIDTH} bytes for {CHUNK_FRAMES} frames × {channels}ch)")
    print(f"{label} first {n_print} int16 samples : {first8}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    pa = pyaudio.PyAudio()
    try:
        print_device_info(pa)
        for ch in CHANNEL_CONFIGS:
            probe_channels(pa, ch)
    finally:
        pa.terminate()


if __name__ == "__main__":
    main()
