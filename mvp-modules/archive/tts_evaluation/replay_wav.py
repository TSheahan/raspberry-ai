#!/usr/bin/env python3
"""
replay_wav.py -- WAV replay through multiple audio backends.

Primary purpose: isolate the root cause of audio tearing on bcm2835
(Pi 4 headphone jack) by comparing PortAudio (PyAudio) against direct
ALSA (pyalsaaudio) and subprocess aplay.

Modes:
    (default)       PyAudio frames_per_buffer sweep — session 2 showed
                    this reduces but cannot eliminate tearing.
    --alsaaudio     Direct ALSA via pyalsaaudio — calls snd_pcm_writei()
                    from the main thread, same as aplay.  If clean,
                    PortAudio's callback thread is the confirmed cause.
    --aplay         Pipe PCM to `aplay -D hw:0,0` via subprocess stdin.
                    Sanity backstop; expected clean since aplay works.

Usage:
    # Default PyAudio sweep:
    python replay_wav.py deepgram_00.wav

    # Direct ALSA (requires: pip install pyalsaaudio):
    python replay_wav.py deepgram_00.wav --alsaaudio

    # Subprocess aplay:
    python replay_wav.py deepgram_00.wav --aplay

    # PyAudio with specific values only:
    python replay_wav.py deepgram_00.wav --frames 4096 8192

    # PyAudio streaming simulation:
    python replay_wav.py deepgram_00.wav --chunk

    # ALSA debug capture (Linux only):
    LIBASOUND_DEBUG=1 python replay_wav.py deepgram_00.wav --alsaaudio 2>alsa_debug.txt
"""

import argparse
import subprocess
import sys
import time
import wave

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_DEVICE = 0 if sys.platform != "win32" else None
_DEFAULT_FRAMES_SWEEP = [256, 512, 1024, 2048, 4096, 8192]
_DEFAULT_PAUSE = 2.0
_DEFAULT_ALSA_DEVICE = "hw:0,0"
_DEFAULT_ALSA_PERIOD = 4096


# ---------------------------------------------------------------------------
# PyAudio playback (existing)
# ---------------------------------------------------------------------------

def _device_info_str(pa, device_idx: int | None) -> str:
    idx = device_idx if device_idx is not None else pa.get_default_output_device_info()["index"]
    info = pa.get_device_info_by_index(idx)
    return (
        f"device[{idx}] '{info['name']}'  "
        f"defaultSampleRate={info['defaultSampleRate']:.0f}Hz  "
        f"defaultLowOutputLatency={info['defaultLowOutputLatency']*1000:.1f}ms  "
        f"defaultHighOutputLatency={info['defaultHighOutputLatency']*1000:.1f}ms"
    )


def _play_pyaudio(
    pcm: bytes,
    frame_rate: int,
    n_channels: int,
    sample_width: int,
    frames_per_buffer: int,
    device_idx: int | None,
    chunk_mode: bool,
) -> list[str]:
    """Play via PyAudio. Returns summary lines for aggregation."""
    import pyaudio

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
            negotiated_latency_s = stream.get_output_latency()
            negotiated_frames = round(negotiated_latency_s * frame_rate)
            print(
                f"  negotiated output latency: "
                f"{negotiated_latency_s*1000:.1f}ms  ({negotiated_frames} frames at {frame_rate}Hz)"
            )

            write_times_ms: list[float] = []
            t_start = time.monotonic()

            if chunk_mode:
                chunk_bytes = frames_per_buffer * bytes_per_frame
                offset = 0
                while offset < len(pcm):
                    chunk = pcm[offset : offset + chunk_bytes]
                    t0 = time.monotonic()
                    stream.write(chunk)
                    write_times_ms.append((time.monotonic() - t0) * 1000)
                    offset += chunk_bytes
            else:
                t0 = time.monotonic()
                stream.write(pcm)
                write_times_ms.append((time.monotonic() - t0) * 1000)

            elapsed_s = time.monotonic() - t_start
            remaining_s = duration_s - elapsed_s
            if remaining_s > 0.01:
                print(f"  drain wait: {remaining_s*1000:.0f}ms")
                time.sleep(remaining_s)

        finally:
            stream.stop_stream()
            stream.close()

    finally:
        pa.terminate()

    wall_ms = (time.monotonic() - t_start) * 1000
    summary: list[str] = []

    def _emit(line: str) -> None:
        print(line)
        summary.append(line)

    header = f"pyaudio fpb={frames_per_buffer}"
    if chunk_mode:
        avg_ms = sum(write_times_ms) / len(write_times_ms)
        max_ms = max(write_times_ms)
        slow = [t for t in write_times_ms if t > expected_chunk_ms * 2]
        _emit(
            f"  [{header}]  write() calls: {len(write_times_ms)}  "
            f"avg: {avg_ms:.1f}ms  max: {max_ms:.1f}ms  "
            f"expected per chunk: {expected_chunk_ms:.1f}ms"
        )
        if slow:
            _emit(
                f"  [{header}]  *** {len(slow)} underrun candidate(s): "
                f"{[f'{t:.0f}ms' for t in slow[:10]]} ***"
            )
        else:
            _emit(f"  [{header}]  no slow writes (threshold: {expected_chunk_ms*2:.1f}ms)")
    else:
        _emit(
            f"  [{header}]  single write(): {write_times_ms[0]:.0f}ms  "
            f"wall: {wall_ms:.0f}ms  "
            f"audio duration: {duration_s*1000:.0f}ms"
        )

    return summary


# ---------------------------------------------------------------------------
# pyalsaaudio playback (direct ALSA, no PortAudio)
# ---------------------------------------------------------------------------

def _play_alsaaudio(
    pcm: bytes,
    frame_rate: int,
    n_channels: int,
    sample_width: int,
    alsa_device: str,
    period_size: int,
) -> list[str]:
    """Play via pyalsaaudio — direct snd_pcm_writei(), no callback thread."""
    try:
        import alsaaudio
    except ImportError:
        msg = "  [alsaaudio] package not installed -- run: pip install pyalsaaudio"
        print(msg)
        return [msg]

    fmt_map = {1: alsaaudio.PCM_FORMAT_U8, 2: alsaaudio.PCM_FORMAT_S16_LE}
    pcm_format = fmt_map.get(sample_width)
    if pcm_format is None:
        msg = f"  [alsaaudio] unsupported sample_width={sample_width}"
        print(msg)
        return [msg]

    bytes_per_frame = n_channels * sample_width
    duration_s = len(pcm) / (frame_rate * bytes_per_frame)
    chunk_bytes = period_size * bytes_per_frame

    print(f"\n  alsaaudio  device={alsa_device}  period_size={period_size}")

    device = alsaaudio.PCM(
        type=alsaaudio.PCM_PLAYBACK,
        device=alsa_device,
        channels=n_channels,
        rate=frame_rate,
        format=pcm_format,
        periodsize=period_size,
    )

    write_times_ms: list[float] = []
    t_start = time.monotonic()

    try:
        offset = 0
        while offset < len(pcm):
            chunk = pcm[offset : offset + chunk_bytes]
            t0 = time.monotonic()
            device.write(chunk)
            write_times_ms.append((time.monotonic() - t0) * 1000)
            offset += chunk_bytes
    finally:
        device.close()

    wall_ms = (time.monotonic() - t_start) * 1000
    avg_ms = sum(write_times_ms) / len(write_times_ms) if write_times_ms else 0
    max_ms = max(write_times_ms) if write_times_ms else 0
    expected_chunk_ms = (period_size / frame_rate) * 1000

    summary: list[str] = []

    def _emit(line: str) -> None:
        print(line)
        summary.append(line)

    _emit(
        f"  [alsaaudio dev={alsa_device} period={period_size}]  "
        f"writes: {len(write_times_ms)}  avg: {avg_ms:.1f}ms  max: {max_ms:.1f}ms  "
        f"wall: {wall_ms:.0f}ms  audio: {duration_s*1000:.0f}ms"
    )

    slow = [t for t in write_times_ms if t > expected_chunk_ms * 2]
    if slow:
        _emit(
            f"  [alsaaudio]  *** {len(slow)} slow write(s): "
            f"{[f'{t:.0f}ms' for t in slow[:10]]} ***"
        )

    return summary


# ---------------------------------------------------------------------------
# subprocess aplay playback
# ---------------------------------------------------------------------------

def _play_aplay(
    pcm: bytes,
    frame_rate: int,
    n_channels: int,
    sample_width: int,
    alsa_device: str,
) -> list[str]:
    """Pipe raw PCM to aplay via subprocess. Expected clean (sanity backstop)."""
    fmt_map = {1: "U8", 2: "S16_LE"}
    alsa_fmt = fmt_map.get(sample_width)
    if alsa_fmt is None:
        msg = f"  [aplay] unsupported sample_width={sample_width}"
        print(msg)
        return [msg]

    bytes_per_frame = n_channels * sample_width
    duration_s = len(pcm) / (frame_rate * bytes_per_frame)

    cmd = [
        "aplay",
        "-D", alsa_device,
        "-r", str(frame_rate),
        "-f", alsa_fmt,
        "-c", str(n_channels),
        "-t", "raw",
    ]
    print(f"\n  aplay  cmd: {' '.join(cmd)}")

    t_start = time.monotonic()
    proc = subprocess.run(cmd, input=pcm, capture_output=True)
    wall_ms = (time.monotonic() - t_start) * 1000

    summary: list[str] = []

    def _emit(line: str) -> None:
        print(line)
        summary.append(line)

    if proc.returncode == 0:
        _emit(
            f"  [aplay dev={alsa_device}]  "
            f"wall: {wall_ms:.0f}ms  audio: {duration_s*1000:.0f}ms  exit: 0"
        )
    else:
        stderr_snippet = proc.stderr.decode(errors="replace").strip()[:200]
        _emit(
            f"  [aplay dev={alsa_device}]  "
            f"FAILED exit={proc.returncode}  stderr: {stderr_snippet}"
        )

    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay a WAV through PyAudio, pyalsaaudio, or aplay to isolate tearing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("wav", help="WAV file path (e.g. deepgram_00.wav)")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--alsaaudio", action="store_true",
        help="Play via pyalsaaudio (direct ALSA, no PortAudio). "
             "Requires: pip install pyalsaaudio",
    )
    mode.add_argument(
        "--aplay", action="store_true",
        help="Pipe PCM to aplay subprocess (sanity backstop).",
    )

    p.add_argument(
        "--frames", type=int, nargs="+", metavar="N",
        help=(
            "PyAudio frames_per_buffer values to sweep "
            f"(default: {_DEFAULT_FRAMES_SWEEP})"
        ),
    )
    p.add_argument(
        "--chunk", action="store_true",
        help="PyAudio mode: write frames_per_buffer-sized chunks (streaming simulation).",
    )
    p.add_argument(
        "--alsa-device", default=_DEFAULT_ALSA_DEVICE, metavar="DEV",
        help=f"ALSA device for --alsaaudio/--aplay (default: {_DEFAULT_ALSA_DEVICE})",
    )
    p.add_argument(
        "--alsa-period", type=int, default=_DEFAULT_ALSA_PERIOD, metavar="N",
        help=f"Period size for --alsaaudio (default: {_DEFAULT_ALSA_PERIOD})",
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

    aggregate: list[str] = []

    if args.alsaaudio:
        print(f"Mode: alsaaudio (direct ALSA)  device={args.alsa_device}  period={args.alsa_period}")
        print()
        lines = _play_alsaaudio(
            pcm=pcm,
            frame_rate=frame_rate,
            n_channels=n_channels,
            sample_width=sample_width,
            alsa_device=args.alsa_device,
            period_size=args.alsa_period,
        )
        aggregate.extend(lines)

    elif args.aplay:
        print(f"Mode: aplay subprocess  device={args.alsa_device}")
        print()
        lines = _play_aplay(
            pcm=pcm,
            frame_rate=frame_rate,
            n_channels=n_channels,
            sample_width=sample_width,
            alsa_device=args.alsa_device,
        )
        aggregate.extend(lines)

    else:
        frames_list = args.frames or _DEFAULT_FRAMES_SWEEP
        mode_label = "chunk-by-chunk (streaming simulation)" if args.chunk else "single write"
        print(f"Mode: PyAudio {mode_label}")
        print(f"frames_per_buffer sweep: {frames_list}")
        print(f"Device index: {args.device}")
        print()
        print("Listen for tearing with each value. Ctrl+C to stop early.")

        for fpb in frames_list:
            try:
                lines = _play_pyaudio(
                    pcm=pcm,
                    frame_rate=frame_rate,
                    n_channels=n_channels,
                    sample_width=sample_width,
                    frames_per_buffer=fpb,
                    device_idx=args.device,
                    chunk_mode=args.chunk,
                )
                aggregate.extend(lines)
            except KeyboardInterrupt:
                print("\nStopped early.")
                break
            time.sleep(args.pause)

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    for line in aggregate:
        print(line)
    print("=" * 70)


if __name__ == "__main__":
    main()
