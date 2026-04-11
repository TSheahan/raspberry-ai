#!/usr/bin/env python3
"""
Live ALSA capture + PCM stats (same metrics as mvp-modules/vad-only/analyze_dump.py).

Use this to iterate mixer / capture settings without writing a dump file first:
adjust levels, run one command, read the report.

Capture path: arecord → raw S16_LE (same layout as pipeline PCM dumps from
``assistant/frame_dump.py`` / the voice assistant, analysable with ``vad-only/analyze_dump.py``).

For apples-to-apples with in-pipeline captures, use the same ALSA card/device the recorder uses
(``arecord -l`` / PyAudio device selection).

Usage:
    source ~/venv/bin/activate
    python mvp-modules/signal_levels/capture_stats.py
    python mvp-modules/signal_levels/capture_stats.py -D hw:3,0 --seconds 10
    python mvp-modules/signal_levels/capture_stats.py -D hw:3,0 --json --no-human
    python mvp-modules/signal_levels/capture_stats.py --save ~/last_level_check.pcm
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
from pathlib import Path


def _default_alsa_device() -> str | None:
    d = os.environ.get("SIGNAL_LEVELS_DEVICE")
    return d if d else None


def _vad_only_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "vad-only"


def _import_analyze():
    vad = _vad_only_dir()
    if str(vad) not in sys.path:
        sys.path.insert(0, str(vad))
    try:
        from analyze_dump import analyze, print_report  # type: ignore
    except ImportError as e:
        print(f"Error: could not import analyze_dump from {vad}: {e}", file=sys.stderr)
        sys.exit(1)
    return analyze, print_report


def capture_arecord(
    *,
    device: str | None,
    seconds: float,
    sample_rate: int,
    channels: int,
    out_path: Path,
) -> None:
    """Record raw S16_LE PCM via arecord. ``seconds`` is rounded up to a whole second."""
    dur = max(1, int(math.ceil(seconds)))
    cmd = [
        "arecord",
        "-q",
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        str(channels),
        "-d",
        str(dur),
    ]
    if device:
        cmd.extend(["-D", device])
    cmd.append(str(out_path))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        print(f"arecord failed: {err}", file=sys.stderr)
        print(f"Command: {' '.join(cmd)}", file=sys.stderr)
        if "hannels" in err and channels == 1:
            print(
                "Hint: this device may not expose mono at the hardware PCM. "
                "Retry with `-D plughw:...` or `--channels 2` (L/R averaged for stats).",
                file=sys.stderr,
            )
        sys.exit(proc.returncode or 1)


def _mono_analyze_path(raw_path: Path, channels: int) -> tuple[Path, Path | None]:
    """Return (path_to_pass_to_analyze, optional_mono_temp_to_delete).

    ``analyze_dump`` assumes mono int16; stereo captures are averaged L/R.
    """
    if channels == 1:
        return raw_path, None
    raw = np.fromfile(raw_path, dtype=np.int16)
    if len(raw) < 2:
        return raw_path, None
    n_pairs = len(raw) // 2
    mono = (
        raw[: n_pairs * 2]
        .reshape(n_pairs, 2)
        .astype(np.float64)
        .mean(axis=1)
        .astype(np.int16)
    )
    mono_path = raw_path.with_suffix(".mono_for_stats.pcm")
    mono.tofile(mono_path)
    return mono_path, mono_path


def report_to_jsonable(r: dict) -> dict:
    """Convert analyze_dump report for json.dumps (spectral keys are tuples)."""
    out = {}
    for k, v in r.items():
        if k == "spectral":
            out[k] = {f"{lo}-{hi}": val for (lo, hi), val in v.items()}
        elif k == "dc_range" and isinstance(v, tuple):
            out[k] = [v[0], v[1]]
        else:
            out[k] = v
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture from ALSA (arecord) and print PCM level / quality stats.",
    )
    p.add_argument(
        "-D",
        "--device",
        default=_default_alsa_device(),
        metavar="NAME",
        help="ALSA capture device (e.g. hw:3,0). Default: $SIGNAL_LEVELS_DEVICE or system default.",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help="Capture length in seconds (rounded up for arecord -d, minimum 1).",
    )
    p.add_argument("--rate", type=int, default=16_000, help="Sample rate (default 16000).")
    p.add_argument(
        "--channels",
        type=int,
        default=1,
        choices=(1, 2),
        help="Channel count (default 1).",
    )
    p.add_argument(
        "--save",
        type=Path,
        default=None,
        metavar="PATH",
        help="Keep the raw capture at PATH (otherwise temporary file is deleted).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON (full report).",
    )
    p.add_argument(
        "--no-human",
        action="store_true",
        help="Skip human-readable report (use with --json).",
    )
    p.add_argument(
        "--list-devices",
        action="store_true",
        help="Run `arecord -L` and exit.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_devices:
        subprocess.run(["arecord", "-L"], check=False)
        return

    analyze, print_report = _import_analyze()

    out_path: Path | None = None
    temp_path: Path | None = None
    mono_temp: Path | None = None
    try:
        if args.save:
            out_path = args.save.expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            fd, name = tempfile.mkstemp(suffix=".pcm", prefix="capture_stats_")
            os.close(fd)
            temp_path = Path(name)
            out_path = temp_path

        assert out_path is not None
        capture_arecord(
            device=args.device,
            seconds=args.seconds,
            sample_rate=args.rate,
            channels=args.channels,
            out_path=out_path,
        )

        analyze_path, mono_temp = _mono_analyze_path(out_path, args.channels)
        report = analyze(analyze_path)
        report["capture_device"] = args.device if args.device else "(default)"
        report["capture_channels"] = args.channels
        if mono_temp is not None:
            report["note"] = "stereo capture; stats computed on L/R mean (mono)"

        if args.json:
            print(json.dumps(report_to_jsonable(report), indent=2))

        if not args.no_human:
            label = report["capture_device"]
            report["path"] = f"live:{label}" + (f" @ {out_path}" if args.save else "")
            print("=" * 72)
            print(f"Live capture  {label}  ({report['duration_s']:.1f} s)")
            print("=" * 72)
            print_report(report)
            if args.save:
                print(f"Saved raw PCM: {out_path}")
    finally:
        for p in (mono_temp, temp_path):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
