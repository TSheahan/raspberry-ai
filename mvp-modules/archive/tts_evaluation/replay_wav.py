#!/usr/bin/env python3
"""
replay_wav.py -- PyAudio WAV replay with configurable frames_per_buffer.

Isolates PyAudio tearing from TTS API content and latency.  Reads any
WAV file (e.g. one saved by compare_tts.py --save-wav) and plays it
repeatedly with different frames_per_buffer values.  Reports the
PortAudio-negotiated output latency and per-write() timing so you can
see whether underruns explain the tearing.

Usage:
    # Sweep all default frames_per_buffer values (256 -> 8192):
    python replay_wav.py deepgram_00.wav

    # Try specific values only:
    python replay_wav.py deepgram_00.wav --frames 4096 8192

    # Simulate streaming: write N frames at a time instead of one big write:
    python replay_wav.py deepgram_00.wav --chunk

    # Capture full ALSA negotiation to a file (Linux only):
    LIBASOUND_DEBUG=1 python replay_wav.py deepgram_00.wav 2>alsa_debug.txt
    grep -E "period_size|buffer_size|hw_params" alsa_debug.txt

Device:
    Default device index is 0 (bcm2835 ALSA headphones) on Linux, and
    the PortAudio default (None) on Windows.  Override with --device IDX.

What to look for:
    - "negotiated output latency" -- PortAudio's settled period in frames.
      aplay defaults to ~5461 frames (~227ms) at 24kHz.  If PortAudio
      settles much lower (e.g. 512 frames / 21ms) underruns are likely.
    - Slow write() calls in --chunk mode (marked with ***) indicate the
      hardware buffer emptied before more data arrived -- that IS tearing.
    - The first frames_per_buffer value that plays without tearing is the
      fix candidate; apply it to compare_tts.py via --frames-per-buffer.
"""

import argparse
import sys
import time
import wave

import pyaudio

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_DEVICE = 0 if sys.platform != "win32" else None
_DEFAULT_FRAMES_SWEEP = [256, 512, 1024, 2048, 4096, 8192]
_DEFAULT_PAUSE = 2.0  # seconds between sweep attempts


# ---------------------------------------------------------------------------
# Core playback
# ---------------------------------------------------------------------------

def _device_info_str(pa: pyaudio.PyAudio, device_idx: int | None) -> str:
    idx = device_idx if device_idx is not None else pa.get_default_output_device_info()["index"]
    info = pa.get_device_info_by_index(idx)
    return (
        f"device[{idx}] '{info['name']}'  "
        f"defaultSampleRate={info['defaultSampleRate']:.0f}Hz  "
        f"defaultLowOutputLatency={info['defaultLowOutputLatency']*1000:.1f}ms  "
        f"defaultHighOutputLatency={info['defaultHighOutputLatency']*1000:.1f}ms"
    )


def _play_once(
    pcm: bytes,
    frame_rate: int,
    n_channels: int,
    sample_width: int,
    frames_per_buffer: int,
    device_idx: int | None,
    chunk_mode: bool,
) -> None:
    """Open a PyAudio stream, play pcm, print timing diagnostics, close."""
    bytes_per_frame = n_channels * sample_width
    duration_s = len(pcm) / (frame_rate * bytes_per_frame)
    expected_chunk_ms = (frames_per_buffer / frame_rate) * 1000

    pa = pyaudio.PyAudio()
    try:
        print(f"\n  frames_per_buffer = {frames_per_buffer}")
        print(f"  {_device_info_str(pa, device_idx)}")

        stream = pa.open(
            format=pa.get_format_from_width(sample_width),
            channels=n_channels,
            rate=frame_rate,
            output=True,
            output_device_index=device_idx,
            frames_per_buffer=frames_per_buffer,
        )
        try:
            # PortAudio reports the actual negotiated latency after open().
            # Multiply by sample rate to get the settled period in frames.
            negotiated_latency_s = stream.get_output_latency()
            negotiated_frames = round(negotiated_latency_s * frame_rate)
            print(
                f"  negotiated output latency: "
                f"{negotiated_latency_s*1000:.1f}ms  ({negotiated_frames} frames at {frame_rate}Hz)"
            )

            write_times_ms: list[float] = []
            t_start = time.monotonic()

            if chunk_mode:
                # Simulate streaming: write one frames_per_buffer chunk at a time.
                # Slow write() calls (>2x expected) flag likely hardware underruns.
                chunk_bytes = frames_per_buffer * bytes_per_frame
                offset = 0
                while offset < len(pcm):
                    chunk = pcm[offset : offset + chunk_bytes]
                    t0 = time.monotonic()
                    stream.write(chunk)
                    write_times_ms.append((time.monotonic() - t0) * 1000)
                    offset += chunk_bytes
            else:
                # Single write: entire PCM block at once (Deepgram REST case).
                t0 = time.monotonic()
                stream.write(pcm)
                write_times_ms.append((time.monotonic() - t0) * 1000)

        finally:
            stream.stop_stream()
            stream.close()

    finally:
        pa.terminate()

    wall_ms = (time.monotonic() - t_start) * 1000

    if chunk_mode:
        avg_ms = sum(write_times_ms) / len(write_times_ms)
        max_ms = max(write_times_ms)
        # A write() that takes more than twice the expected chunk duration
        # means the hardware buffer ran dry before the call returned —
        # a confirmed underrun event.
        slow = [t for t in write_times_ms if t > expected_chunk_ms * 2]
        print(
            f"  write() calls: {len(write_times_ms)}  "
            f"avg: {avg_ms:.1f}ms  max: {max_ms:.1f}ms  "
            f"expected per chunk: {expected_chunk_ms:.1f}ms"
        )
        if slow:
            print(
                f"  *** {len(slow)} underrun candidate(s): "
                f"{[f'{t:.0f}ms' for t in slow[:10]]} ***"
            )
        else:
            print(f"  no slow writes detected (threshold: {expected_chunk_ms*2:.1f}ms)")
    else:
        print(
            f"  single write(): {write_times_ms[0]:.0f}ms  "
            f"wall: {wall_ms:.0f}ms  "
            f"audio duration: {duration_s*1000:.0f}ms"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay a WAV file via PyAudio with configurable frames_per_buffer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("wav", help="WAV file path (e.g. deepgram_00.wav)")
    p.add_argument(
        "--frames", type=int, nargs="+", metavar="N",
        help=(
            "frames_per_buffer values to try in sequence "
            f"(default: sweep {_DEFAULT_FRAMES_SWEEP})"
        ),
    )
    p.add_argument(
        "--chunk", action="store_true",
        help=(
            "Simulate streaming: write frames_per_buffer-sized chunks one at a time. "
            "Enables per-write timing and underrun detection."
        ),
    )
    p.add_argument(
        "--pause", type=float, default=_DEFAULT_PAUSE, metavar="SECS",
        help=f"Pause between sweep attempts (default: {_DEFAULT_PAUSE}s)",
    )
    p.add_argument(
        "--device", type=int, default=_DEFAULT_DEVICE, metavar="IDX",
        help=f"PyAudio output device index (default: {_DEFAULT_DEVICE})",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    frames_list = args.frames or _DEFAULT_FRAMES_SWEEP
    mode_label = "chunk-by-chunk (streaming simulation)" if args.chunk else "single write (all bytes at once)"

    with wave.open(args.wav, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        frame_rate = wf.getframerate()
        n_frames = wf.getnframes()
        pcm = wf.readframes(n_frames)

    duration_s = n_frames / frame_rate
    print(f"WAV: {args.wav}")
    print(f"  {frame_rate}Hz  {n_channels}ch  {sample_width*8}bit  "
          f"{duration_s:.2f}s  {len(pcm)//1024}KB")
    print(f"Mode: {mode_label}")
    print(f"frames_per_buffer sweep: {frames_list}")
    print(f"Device index: {args.device}")
    print()
    print("Listen for tearing with each value.")
    print("The first clean playback identifies the fix candidate.")
    print("Ctrl+C to stop early.")

    for fpb in frames_list:
        try:
            _play_once(
                pcm=pcm,
                frame_rate=frame_rate,
                n_channels=n_channels,
                sample_width=sample_width,
                frames_per_buffer=fpb,
                device_idx=args.device,
                chunk_mode=args.chunk,
            )
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        time.sleep(args.pause)

    print("\nSweep complete.")


if __name__ == "__main__":
    main()
