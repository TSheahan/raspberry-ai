"""
Smoke test: seeed-2mic-voicecard audio capture.

Records a short stereo clip via the WM8960 codec at 16 kHz, shows per-second
RMS levels for each channel, saves a timestamped WAV, then plays it back
through the bcm2835 headphone jack (card 0).

Adapted from forked_assistant/test/capture_playback.py for the 2-mic HAT.

Run:
    cd ~/raspberry-ai
    source ~/venv/bin/activate
    python mvp-modules/2-mic/smoke_capture.py
"""

import math
import struct
import subprocess
import time
import wave
from datetime import datetime

import pyaudio

INPUT_DEVICE   = 1        # seeed-2mic-voicecard (hw:3,0)
OUTPUT_DEVICE  = 0        # bcm2835 headphones   (hw:0,0)
ALSA_CARD      = "3"
SAMPLE_RATE    = 16_000
CHANNELS       = 2        # 2-mic HAT is natively stereo
FORMAT         = pyaudio.paInt16
SAMPLE_WIDTH   = 2
CHUNK_FRAMES   = 512
RECORD_SECONDS = 5
RMS_BAR_WIDTH  = 32


def _rms(samples: list[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _bar(value: float, peak: float) -> str:
    filled = int(RMS_BAR_WIDTH * min(value / peak, 1.0)) if peak > 0 else 0
    return "[" + "#" * filled + "." * (RMS_BAR_WIDTH - filled) + "]"


def show_device_info(pa: pyaudio.PyAudio) -> None:
    info = pa.get_device_info_by_index(INPUT_DEVICE)
    print(f"[DEV]  index={INPUT_DEVICE}  name={info['name']}")
    print(f"[DEV]  maxInputChannels={info['maxInputChannels']}  "
          f"maxOutputChannels={info['maxOutputChannels']}  "
          f"defaultSampleRate={info['defaultSampleRate']}")
    print()


def show_capture_gain() -> None:
    print(f"[GAIN] Capture controls (amixer -c {ALSA_CARD}):")
    result = subprocess.run(
        ["amixer", "-c", ALSA_CARD, "sget", "Capture"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for ln in result.stdout.splitlines():
            print(f"  {ln.strip()}")
    else:
        print(f"  FAILED: {result.stderr.strip()}")
    print()


def deinterleave(raw: bytes, n_frames: int) -> tuple[list[int], list[int]]:
    """Split interleaved stereo int16 into per-channel sample lists."""
    samples = struct.unpack_from(f"<{n_frames * 2}h", raw)
    left  = list(samples[0::2])
    right = list(samples[1::2])
    return left, right


def record(pa: pyaudio.PyAudio) -> tuple[bytes, dict]:
    stream = pa.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=INPUT_DEVICE,
        frames_per_buffer=CHUNK_FRAMES,
    )

    chunks: list[bytes] = []
    window_l: list[int] = []
    window_r: list[int] = []
    per_sec: list[tuple[float, float]] = []
    samples_per_sec = SAMPLE_RATE

    n_chunks = int(SAMPLE_RATE / CHUNK_FRAMES * RECORD_SECONDS)

    print(f"[REC]  Recording {RECORD_SECONDS}s — stereo, {SAMPLE_RATE} Hz, int16")
    print(f"[REC]  {' ':6s}  {'Left':>{RMS_BAR_WIDTH + 10}s}  {'Right':>{RMS_BAR_WIDTH + 10}s}")
    print()

    for _ in range(n_chunks):
        raw = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
        chunks.append(raw)
        left, right = deinterleave(raw, CHUNK_FRAMES)
        window_l.extend(left)
        window_r.extend(right)

        if len(window_l) >= samples_per_sec:
            per_sec.append((_rms(window_l[:samples_per_sec]),
                            _rms(window_r[:samples_per_sec])))
            window_l = window_l[samples_per_sec:]
            window_r = window_r[samples_per_sec:]

    if window_l:
        per_sec.append((_rms(window_l), _rms(window_r)))

    stream.stop_stream()
    stream.close()

    peak = max(max(l, r) for l, r in per_sec) if per_sec else 1.0
    for i, (l, r) in enumerate(per_sec):
        print(f"  {i+1:2d}s  L {_bar(l, peak)} {l:7.1f}  "
              f"R {_bar(r, peak)} {r:7.1f}")

    stats = {
        "peak_rms":   peak,
        "per_sec":    per_sec,
        "clip_count": 0,  # could count >32700, kept simple for smoke
    }
    return b"".join(chunks), stats


def save_wav(raw: bytes) -> str:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"/tmp/2mic_capture_{ts}.wav"
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return path


def play(pa: pyaudio.PyAudio, raw: bytes) -> None:
    print("[PLAY] Playing back (mono downmix → headphone jack)...")
    n_samples = len(raw) // SAMPLE_WIDTH
    stereo = struct.unpack_from(f"<{n_samples}h", raw)
    left  = stereo[0::2]
    right = stereo[1::2]
    mono = struct.pack(f"<{len(left)}h",
                       *((l + r) // 2 for l, r in zip(left, right)))

    stream = pa.open(
        format=FORMAT,
        channels=1,
        rate=SAMPLE_RATE,
        output=True,
        output_device_index=OUTPUT_DEVICE,
        frames_per_buffer=CHUNK_FRAMES,
    )
    chunk_bytes = CHUNK_FRAMES * SAMPLE_WIDTH
    offset = 0
    while offset < len(mono):
        stream.write(mono[offset:offset + chunk_bytes])
        offset += chunk_bytes
    stream.stop_stream()
    stream.close()
    print("[PLAY] Done.\n")


def main() -> None:
    pa = pyaudio.PyAudio()
    try:
        show_device_info(pa)
        show_capture_gain()

        print("[REC]  Speak or tap during recording.")
        for i in range(3, 0, -1):
            print(f"[REC]  Starting in {i}...")
            time.sleep(1.0)
        print()

        raw, stats = record(pa)
        print(f"\n[STAT] peak_rms={stats['peak_rms']:.1f}")
        if stats["peak_rms"] < 50:
            print("[STAT] NOTE: very low level — check Capture volume")
        print()

        wav_path = save_wav(raw)
        print(f"[WAV]  Saved: {wav_path}\n")

        play(pa, raw)
    finally:
        pa.terminate()

    print("[DONE] 2-mic HAT smoke test complete.")


if __name__ == "__main__":
    main()
