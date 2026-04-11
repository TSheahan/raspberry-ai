#!/usr/bin/env python3
"""
Analyse a PCM frame dump (16 kHz, mono, int16 LE).

Usage:
    source ~/venv/bin/activate
    python mvp-modules/vad-only/analyze_dump.py ~/pipeline_dump_20260411_104958.pcm
    python mvp-modules/vad-only/analyze_dump.py ~/dump1.pcm ~/dump2.pcm   # side-by-side
"""

import sys
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 320  # 20 ms


def analyze(path: Path) -> dict:
    raw = np.fromfile(path, dtype=np.int16)
    samples = raw.astype(np.float64)
    n_frames = len(raw) // FRAME_SAMPLES
    remainder = len(raw) % FRAME_SAMPLES
    frames = raw[: n_frames * FRAME_SAMPLES].reshape(n_frames, FRAME_SAMPLES).astype(np.float64)
    rms_per_frame = np.sqrt(np.mean(frames ** 2, axis=1))
    dc_per_frame = np.mean(frames, axis=1)

    n_secs = len(raw) // SAMPLE_RATE
    dc_per_sec = [
        float(np.mean(samples[s * SAMPLE_RATE : (s + 1) * SAMPLE_RATE]))
        for s in range(n_secs)
    ]

    # Spectral energy bands
    fft = np.fft.rfft(samples)
    power = np.abs(fft) ** 2
    freqs = np.fft.rfftfreq(len(samples), 1 / SAMPLE_RATE)
    total_power = power.sum()
    bands = [(0, 100), (100, 300), (300, 1000), (1000, 3000), (3000, 8000)]
    spectral = {}
    for lo, hi in bands:
        mask = (freqs >= lo) & (freqs < hi)
        spectral[(lo, hi)] = float(power[mask].sum() / total_power) if total_power > 0 else 0.0

    # Startup transient (first 5 frames)
    startup = []
    for i in range(min(5, n_frames)):
        f = frames[i]
        startup.append({
            "frame": i,
            "mean": float(np.mean(f)),
            "rms": float(np.sqrt(np.mean(f ** 2))),
            "min": int(f.min()),
            "max": int(f.max()),
        })

    return {
        "path": str(path),
        "size_bytes": len(raw) * 2,
        "total_samples": len(raw),
        "duration_s": len(raw) / SAMPLE_RATE,
        "n_frames": n_frames,
        "remainder_bytes": remainder * 2,
        "global_rms": float(np.sqrt(np.mean(samples ** 2))),
        "global_std": float(np.std(samples)),
        "global_dc": float(np.mean(samples)),
        "min_sample": int(raw.min()),
        "max_sample": int(raw.max()),
        "clipped": int(np.sum(np.abs(raw) >= 32767)),
        "median_frame_rms": float(np.median(rms_per_frame)),
        "mean_frame_rms": float(np.mean(rms_per_frame)),
        "pct_rms_gt_2000": float(np.sum(rms_per_frame > 2000) / n_frames),
        "pct_rms_gt_1000": float(np.sum(rms_per_frame > 1000) / n_frames),
        "pct_rms_lt_500": float(np.sum(rms_per_frame < 500) / n_frames),
        "pct_rms_lt_100": float(np.sum(rms_per_frame < 100) / n_frames),
        "rms_percentiles": {
            p: float(np.percentile(rms_per_frame, p)) for p in [5, 10, 25, 50, 75, 90, 95]
        },
        "dc_per_sec": dc_per_sec,
        "dc_range": (min(dc_per_sec), max(dc_per_sec)) if dc_per_sec else (0, 0),
        "spectral": spectral,
        "startup": startup,
    }


def fmt_pct(v: float) -> str:
    return f"{100 * v:.1f} %"


def print_report(r: dict):
    print(f"File: {r['path']}")
    print(f"Size: {r['size_bytes']:,} bytes")
    print(f"Duration: {r['duration_s']:.1f} s ({r['n_frames']} frames × 20 ms)")
    if r["remainder_bytes"]:
        print(f"  ⚠ remainder: {r['remainder_bytes']} bytes")
    print()

    print("Amplitude")
    print(f"  Global RMS:       {r['global_rms']:,.0f}")
    print(f"  Global DC:        {r['global_dc']:+.1f}")
    print(f"  Min / Max sample: {r['min_sample']:,} / {r['max_sample']:,}")
    print(f"  Clipped (±32 767): {r['clipped']}")
    print()

    print("Per-frame RMS")
    print(f"  Median:   {r['median_frame_rms']:,.0f}")
    print(f"  Mean:     {r['mean_frame_rms']:,.0f}")
    print(f"  > 2 000:  {fmt_pct(r['pct_rms_gt_2000'])}")
    print(f"  > 1 000:  {fmt_pct(r['pct_rms_gt_1000'])}")
    print(f"  < 500:    {fmt_pct(r['pct_rms_lt_500'])}")
    print(f"  < 100:    {fmt_pct(r['pct_rms_lt_100'])}")
    print(f"  Percentiles: ", end="")
    print("  ".join(f"p{p}={v:.0f}" for p, v in r["rms_percentiles"].items()))
    print()

    print("DC offset (per second)")
    lo, hi = r["dc_range"]
    print(f"  Range: {lo:+.0f} to {hi:+.0f}")
    for i, dc in enumerate(r["dc_per_sec"]):
        print(f"  sec {i:>2}: {dc:+.1f}")
    print()

    print("Spectral energy")
    for (lo, hi), pct in r["spectral"].items():
        print(f"  {lo:>5}–{hi:<5} Hz: {100 * pct:5.1f} %")
    print()

    print("Startup transient (first 5 frames)")
    for s in r["startup"]:
        print(f"  frame {s['frame']}: mean={s['mean']:+.0f}  rms={s['rms']:,.0f}  "
              f"range=[{s['min']:,}, {s['max']:,}]")
    print()


def print_comparison(reports: list[dict]):
    labels = [Path(r["path"]).stem for r in reports]
    col_w = max(len(l) for l in labels) + 2

    def row(metric, values):
        vals = "".join(f"{v:>{col_w}}" for v in values)
        print(f"  {metric:<28}{vals}")

    header = "".join(f"{l:>{col_w}}" for l in labels)
    print(f"  {'':28}{header}")
    print(f"  {'—' * (28 + col_w * len(labels))}")

    row("Duration (s)", [f"{r['duration_s']:.1f}" for r in reports])
    row("Global RMS", [f"{r['global_rms']:,.0f}" for r in reports])
    row("Global DC", [f"{r['global_dc']:+.1f}" for r in reports])
    row("Median frame RMS", [f"{r['median_frame_rms']:,.0f}" for r in reports])
    row("Frames RMS > 2 000", [fmt_pct(r["pct_rms_gt_2000"]) for r in reports])
    row("Frames RMS > 1 000", [fmt_pct(r["pct_rms_gt_1000"]) for r in reports])
    row("Frames RMS < 500", [fmt_pct(r["pct_rms_lt_500"]) for r in reports])
    row("Frames RMS < 100", [fmt_pct(r["pct_rms_lt_100"]) for r in reports])
    row("Clipped samples", [str(r["clipped"]) for r in reports])
    row("DC range", [f"{r['dc_range'][0]:+.0f} to {r['dc_range'][1]:+.0f}" for r in reports])
    row("Startup frame 0 mean", [f"{r['startup'][0]['mean']:+.0f}" if r["startup"] else "—" for r in reports])
    print()

    print("  Spectral energy")
    for (lo, hi) in [(0, 100), (100, 300), (300, 1000), (1000, 3000), (3000, 8000)]:
        row(f"  {lo}–{hi} Hz", [f"{100 * r['spectral'].get((lo, hi), 0):.1f} %" for r in reports])
    print()


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <dump.pcm> [dump2.pcm ...]", file=sys.stderr)
        sys.exit(1)

    paths = [Path(p).expanduser() for p in sys.argv[1:]]
    for p in paths:
        if not p.exists():
            print(f"Error: {p} not found", file=sys.stderr)
            sys.exit(1)

    reports = [analyze(p) for p in paths]

    for r in reports:
        print("=" * 72)
        print_report(r)

    if len(reports) > 1:
        print("=" * 72)
        print("COMPARISON")
        print("=" * 72)
        print_comparison(reports)


if __name__ == "__main__":
    main()
