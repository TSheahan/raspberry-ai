"""
Audio capture/playback quality probe.

Records a short clip via the 1-ch 16kHz production path, shows per-second RMS
levels, saves a timestamped WAV, then plays it back. Run before and after PGA
gain changes to assess quality impact.

Workflow:
    python test/capture_playback.py            # record + play at current gain
    amixer -c 3 sset 'ADC1 PGA gain' 20        # adjust gain
    python test/capture_playback.py            # compare

Output WAV is saved to /tmp/ and the path is printed for manual inspection.
No inference. No pipeline. Direct PyAudio only.
"""

import math
import os
import struct
import subprocess
import time
import wave
from datetime import datetime

import pyaudio

INPUT_DEVICE   = 1        # ReSpeaker (hw:3,0)
OUTPUT_DEVICE  = 0        # bcm2835 headphones
SAMPLE_RATE    = 16_000
CHANNELS       = 1
FORMAT         = pyaudio.paInt16
SAMPLE_WIDTH   = 2
CHUNK_FRAMES   = 512      # ~32 ms per chunk at 16 kHz
RECORD_SECONDS = 5
RMS_BAR_WIDTH  = 32
ALSA_CARD      = "3"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rms(samples: list[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _rms_bar(value: float, peak: float) -> str:
    filled = int(RMS_BAR_WIDTH * min(value / peak, 1.0)) if peak > 0 else 0
    return "[" + "#" * filled + "." * (RMS_BAR_WIDTH - filled) + "]"


# ---------------------------------------------------------------------------
# Gain readout
# ---------------------------------------------------------------------------

def show_pga_gains() -> None:
    print("[GAIN] Current ADC PGA gain settings (amixer -c 3):")
    result = subprocess.run(
        ["amixer", "-c", ALSA_CARD, "sget", "ADC1 PGA gain"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for ln in result.stdout.splitlines():
            print(f"  {ln.strip()}")
    else:
        # Fall back: print all PGA controls
        result2 = subprocess.run(
            ["amixer", "-c", ALSA_CARD],
            capture_output=True, text=True,
        )
        for ln in result2.stdout.splitlines():
            if "PGA" in ln or "Gain" in ln or "value" in ln.lower():
                print(f"  {ln.strip()}")
    print()


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

def record(pa: pyaudio.PyAudio) -> tuple[bytes, dict]:
    """Record RECORD_SECONDS of 1-ch 16kHz audio.

    Returns (raw_pcm_bytes, stats_dict).
    stats_dict keys: peak_rms, avg_rms, clip_count, per_second_rms.
    """
    stream = pa.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=INPUT_DEVICE,
        frames_per_buffer=CHUNK_FRAMES,
    )

    chunks: list[bytes] = []
    samples_per_second = SAMPLE_RATE
    window_samples: list[int] = []
    per_second_rms: list[float] = []
    all_samples: list[int] = []
    clip_count = 0

    n_chunks = int(SAMPLE_RATE / CHUNK_FRAMES * RECORD_SECONDS)

    print(f"[REC]  Recording {RECORD_SECONDS}s  (one bar = 1 second)")
    print(f"[REC]  Input device {INPUT_DEVICE}, {SAMPLE_RATE} Hz, mono, int16\n")

    for i in range(n_chunks):
        raw     = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
        samples = list(struct.unpack_from(f"<{CHUNK_FRAMES}h", raw))
        chunks.append(raw)
        window_samples.extend(samples)
        all_samples.extend(samples)
        clip_count += sum(1 for s in samples if abs(s) >= 32700)

        if len(window_samples) >= samples_per_second:
            sec_rms = _rms(window_samples[:samples_per_second])
            per_second_rms.append(sec_rms)
            window_samples = window_samples[samples_per_second:]

    # flush partial last second
    if window_samples:
        per_second_rms.append(_rms(window_samples))

    stream.stop_stream()
    stream.close()

    peak_rms = max(per_second_rms) if per_second_rms else 0.0
    avg_rms  = sum(per_second_rms) / len(per_second_rms) if per_second_rms else 0.0

    for i, rms in enumerate(per_second_rms):
        bar = _rms_bar(rms, peak_rms) if peak_rms > 0 else "[" + "." * RMS_BAR_WIDTH + "]"
        print(f"  {i+1:2d}s  {bar}  {rms:7.1f}")

    stats = {
        "peak_rms":       peak_rms,
        "avg_rms":        avg_rms,
        "clip_count":     clip_count,
        "per_second_rms": per_second_rms,
    }
    return b"".join(chunks), stats


# ---------------------------------------------------------------------------
# Save WAV
# ---------------------------------------------------------------------------

def save_wav(raw: bytes) -> str:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"/tmp/capture_{ts}.wav"
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return path


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def play(pa: pyaudio.PyAudio, raw: bytes) -> None:
    print("[PLAY] Playing back...")
    stream = pa.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        output=True,
        output_device_index=OUTPUT_DEVICE,
        frames_per_buffer=CHUNK_FRAMES,
    )
    offset = 0
    chunk_bytes = CHUNK_FRAMES * SAMPLE_WIDTH
    while offset < len(raw):
        stream.write(raw[offset:offset + chunk_bytes])
        offset += chunk_bytes
    stream.stop_stream()
    stream.close()
    print("[PLAY] Done.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    show_pga_gains()

    print("[REC]  Tap / speak during recording to assess capture level.")
    for i in range(3, 0, -1):
        print(f"[REC]  Starting in {i}...")
        time.sleep(1.0)
    print()

    pa = pyaudio.PyAudio()
    try:
        raw, stats = record(pa)
        print(f"\n[STAT] peak_rms={stats['peak_rms']:.1f}  "
              f"avg_rms={stats['avg_rms']:.1f}  "
              f"clips={stats['clip_count']}")
        if stats["clip_count"] > 0:
            print("[STAT] WARNING: clipping detected — consider reducing PGA gain")
        if stats["peak_rms"] < 50:
            print("[STAT] NOTE: very low level — consider increasing PGA gain")
        print()

        wav_path = save_wav(raw)
        print(f"[WAV]  Saved: {wav_path}\n")

        play(pa, raw)
    finally:
        pa.terminate()

    print("[DONE] Compare peak_rms and listen across gain settings:")
    print("       amixer -c 3 sset 'ADC1 PGA gain' <0..28>")
    print("       python test/capture_playback.py")


if __name__ == "__main__":
    main()
