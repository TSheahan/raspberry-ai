"""
tap_cycle_probe.py — 4-Mic Clockwise Tap Cycle Analyser

Records up to 40 seconds (or until Ctrl+C) of 1-ch audio from the ReSpeaker.
Tap the four mic positions clockwise at ~1-second intervals, repeating the
cycle as many times as you like.

Detection strategy: each tap produces one or more elevated RMS windows in a
cluster; the cluster peak is taken as the tap signal. Taps are then assigned
to mic positions in order (mic1 → mic2 → mic3 → mic4 → mic1 → …).

Post-recording analysis surfaces per-mic averages and cycle-level balance.
A dominant mic (one position consistently louder than others) betrays an
uneven hardware mix.

Run on Pi (from forked_assistant/ directory):
    python test/tap_cycle_probe.py
"""

import math
import struct
import sys

import pyaudio

# ── Hardware config (mirrors smoke_respeaker_channels.py) ───────────────────
DEVICE_INDEX = 1
SAMPLE_RATE  = 16_000
FORMAT       = pyaudio.paInt16
SAMPLE_WIDTH = 2
TAP_WINDOW   = 800          # frames per RMS window (~50 ms at 16 kHz)
MAX_SECONDS  = 40
MIC_COUNT    = 4

# ── Tap detection parameters ─────────────────────────────────────────────────
WARMUP_WINDOWS      = 20    # ~1 s of silence used to estimate noise floor
TAP_SNR_THRESHOLD   = 6.0   # tap peak must exceed noise_floor × this
TAP_MIN_GAP_WINDOWS = 4     # consecutive quiet windows that close a cluster (~200 ms)
FALLBACK_NOISE      = 70.0  # used if warmup returns implausibly low values


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rms(raw: bytes, n_frames: int) -> float:
    samples = struct.unpack_from(f"<{n_frames}h", raw)
    return math.sqrt(sum(s * s for s in samples) / n_frames)


def _detect_taps(
    windows: list[tuple[float, float]], threshold: float
) -> list[tuple[float, float]]:
    """Cluster elevated windows and return (peak_time, peak_rms) per tap."""
    taps: list[tuple[float, float]] = []
    cluster: list[tuple[float, float]] = []
    gap = 0

    for t, rms in windows:
        if rms >= threshold:
            cluster.append((t, rms))
            gap = 0
        elif cluster:
            gap += 1
            if gap >= TAP_MIN_GAP_WINDOWS:
                taps.append(max(cluster, key=lambda x: x[1]))
                cluster = []
                gap = 0

    if cluster:
        taps.append(max(cluster, key=lambda x: x[1]))

    return taps


# ── Analysis ─────────────────────────────────────────────────────────────────

def _print_analysis(taps: list[tuple[float, float]]) -> None:
    if not taps:
        print("\n[CYC] No taps detected — nothing to analyse.")
        return

    n = len(taps)
    n_complete = n // MIC_COUNT

    print(f"\n{'=' * 62}")
    print(f"CYCLE ANALYSIS  —  {n} taps  /  {n_complete} complete {MIC_COUNT}-tap cycle(s)")
    print(f"{'=' * 62}")

    # ── Tap table ─────────────────────────────────────────────────────────
    print(f"  {'Tap':>4}  {'Mic':>3}  {'Cycle':>5}  {'Time':>7}  {'Peak RMS':>9}")
    print("  " + "─" * 38)

    per_mic: dict[int, list[float]] = {m: [] for m in range(1, MIC_COUNT + 1)}

    for idx, (t, rms) in enumerate(taps):
        mic_pos = (idx % MIC_COUNT) + 1
        cycle   = (idx // MIC_COUNT) + 1
        per_mic[mic_pos].append(rms)
        print(f"  {idx+1:>4}  M{mic_pos}  {cycle:>5}  {t:>6.2f}s  {rms:>9.1f}")

    # ── Per-mic summary ───────────────────────────────────────────────────
    print(f"\n  {'─' * 56}")
    print(f"  {'Mic':<6}  {'N':>3}  {'Avg':>9}  {'Min':>9}  {'Max':>9}")
    print(f"  {'─'*6}  {'─'*3}  {'─'*9}  {'─'*9}  {'─'*9}")

    avgs: dict[int, float] = {}
    for mic in range(1, MIC_COUNT + 1):
        vals = per_mic[mic]
        if vals:
            avg = sum(vals) / len(vals)
            avgs[mic] = avg
            print(f"  Mic {mic}  {len(vals):>3}  {avg:>9.1f}  {min(vals):>9.1f}  {max(vals):>9.1f}")
        else:
            print(f"  Mic {mic}  {'0':>3}  {'—':>9}  {'—':>9}  {'—':>9}")

    # ── Cycle balance table ───────────────────────────────────────────────
    if n_complete >= 1:
        print(f"\n  {'─' * 56}")
        mic_hdr = "  ".join(f"{'M'+str(m):>9}" for m in range(1, MIC_COUNT + 1))
        print(f"  {'Cyc':>3}  {mic_hdr}  {'Max/Min':>8}")
        print(f"  {'─'*3}  " + "  ".join(["─" * 9] * MIC_COUNT) + "  " + "─" * 8)

        for cyc in range(n_complete):
            row_vals: list[float | None] = []
            for mic in range(1, MIC_COUNT + 1):
                tap_idx = cyc * MIC_COUNT + (mic - 1)
                row_vals.append(taps[tap_idx][1] if tap_idx < len(taps) else None)
            valid = [v for v in row_vals if v is not None]
            ratio = max(valid) / min(valid) if valid and min(valid) > 0 else 0.0
            cells = "  ".join(
                f"{v:>9.0f}" if v is not None else f"{'—':>9}" for v in row_vals
            )
            print(f"  {cyc+1:>3}  {cells}  {ratio:>8.2f}×")

    # ── Dominance verdict ─────────────────────────────────────────────────
    if avgs:
        dominant   = max(avgs, key=lambda m: avgs[m])
        others_avg = (
            sum(v for m, v in avgs.items() if m != dominant) / (len(avgs) - 1)
            if len(avgs) > 1 else 1.0
        )
        dom_ratio = avgs[dominant] / others_avg if others_avg > 0 else 0.0

        print(f"\n  {'─' * 56}")
        print(f"  Dominant : Mic {dominant}  avg={avgs[dominant]:.0f}"
              f"   Others avg={others_avg:.0f}   Ratio={dom_ratio:.2f}×")

        if dom_ratio > 2.0:
            print(f"\n  UNEVEN — Mic {dominant} is {dom_ratio:.1f}x louder than the rest.")
            print("  Hardware mix is not uniform; one position dominates.")
        elif dom_ratio > 1.4:
            print(f"\n  MODERATE imbalance — Mic {dominant} is {dom_ratio:.1f}x the others' average.")
        else:
            print("\n  EVEN — tap response is consistent across all positions.")

    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    pa     = pyaudio.PyAudio()
    stream = None

    all_windows: list[tuple[float, float]] = []
    noise_floor = FALLBACK_NOISE
    cluster: list[tuple[float, float]] = []
    gap     = 0
    tap_num = 0

    try:
        stream = pa.open(
            format=FORMAT,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=DEVICE_INDEX,
            frames_per_buffer=TAP_WINDOW,
        )

        # ── Warmup: sample noise floor (stay quiet) ───────────────────────
        print("[TAP] Warming up (~1 s) — stay quiet…")
        warmup_rms: list[float] = []
        for i in range(WARMUP_WINDOWS):
            raw = stream.read(TAP_WINDOW, exception_on_overflow=False)
            t   = i * TAP_WINDOW / SAMPLE_RATE
            rms = _rms(raw, TAP_WINDOW)
            all_windows.append((t, rms))
            warmup_rms.append(rms)

        # 90th-percentile of warmup gives a stable ceiling on ambient noise
        warmup_rms.sort()
        noise_floor = warmup_rms[int(len(warmup_rms) * 0.9)]
        if noise_floor < 10.0:
            noise_floor = FALLBACK_NOISE
        threshold = noise_floor * TAP_SNR_THRESHOLD

        ms_per_window = TAP_WINDOW / SAMPLE_RATE * 1000
        print(f"[TAP] Noise floor ~{noise_floor:.0f}  →  tap threshold {threshold:.0f}"
              f"  ({ms_per_window:.0f} ms/window)")
        print(f"[TAP] Tap:  mic1 → mic2 → mic3 → mic4 → mic1 → …  (clockwise)")
        print(f"[TAP] Up to {MAX_SECONDS} s. Ctrl+C to stop early.\n")

        # ── Live recording ─────────────────────────────────────────────────
        t_offset   = WARMUP_WINDOWS * TAP_WINDOW / SAMPLE_RATE
        n_remaining = (MAX_SECONDS * SAMPLE_RATE) // TAP_WINDOW - WARMUP_WINDOWS

        for i in range(n_remaining):
            raw = stream.read(TAP_WINDOW, exception_on_overflow=False)
            t   = t_offset + i * TAP_WINDOW / SAMPLE_RATE
            rms = _rms(raw, TAP_WINDOW)
            all_windows.append((t, rms))

            if rms >= threshold:
                cluster.append((t, rms))
                gap = 0
            elif cluster:
                gap += 1
                if gap >= TAP_MIN_GAP_WINDOWS:
                    peak_t, peak_rms = max(cluster, key=lambda x: x[1])
                    tap_num += 1
                    mic_pos = (tap_num - 1) % MIC_COUNT + 1
                    cycle   = (tap_num - 1) // MIC_COUNT + 1
                    print(f"  tap {tap_num:2d}  mic{mic_pos}  cycle {cycle}"
                          f"  t={peak_t:.2f}s  rms={peak_rms:.0f}")
                    cluster = []
                    gap = 0

        print("\n[TAP] 40 s reached.")

    except KeyboardInterrupt:
        print("\n[TAP] Stopped by user (Ctrl+C).")

    finally:
        # Flush any cluster that was still open when recording ended
        if cluster:
            peak_t, peak_rms = max(cluster, key=lambda x: x[1])
            tap_num += 1
            mic_pos = (tap_num - 1) % MIC_COUNT + 1
            cycle   = (tap_num - 1) // MIC_COUNT + 1
            print(f"  tap {tap_num:2d}  mic{mic_pos}  cycle {cycle}"
                  f"  t={peak_t:.2f}s  rms={peak_rms:.0f}")

        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        pa.terminate()

    # ── Post-recording analysis ────────────────────────────────────────────
    if all_windows:
        taps = _detect_taps(all_windows, noise_floor * TAP_SNR_THRESHOLD)
        _print_analysis(taps)


if __name__ == "__main__":
    main()
