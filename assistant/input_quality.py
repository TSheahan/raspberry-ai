"""
input_quality.py — Inline audio quality monitor for the Pipecat pipeline.

Activated by INPUT_QUALITY_CHECK=1.  Accumulates per-frame RMS statistics
over a configurable window after stream open, then emits a one-time verdict:

  - WARNING if the signal profile matches known-bad conditions (high
    sustained RMS, hard clipping, excessive DC wander) that prevent Silero
    VAD from detecting speech transitions.
  - INFO otherwise (silent in normal logs unless LOG_LEVEL <= DEBUG).

Read-only tap: never alters, drops, or delays any frame.
"""

import os
import struct

from loguru import logger
from pipecat.frames.frames import (
    Frame, AudioRawFrame, InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

WINDOW_SECS = float(os.environ.get("INPUT_QUALITY_WINDOW_SECS", "5"))

CLIP_THRESHOLD = 32767
RMS_HOT_THRESHOLD = 2000
PCT_HOT_WARN = 0.40
CLIP_WARN = 10
DC_WANDER_WARN = 1400


def input_quality_enabled() -> bool:
    return os.environ.get("INPUT_QUALITY_CHECK", "0") == "1"


class InputQualityProcessor(FrameProcessor):
    """Accumulate audio stats over a window, then emit a quality verdict."""

    def __init__(self):
        super().__init__()
        self._sample_rate: int = 0
        self._num_channels: int = 0
        self._window_bytes: int = 0
        self._bytes_seen: int = 0
        self._frame_count: int = 0
        self._done = False

        self._frame_rms: list[float] = []
        self._sec_dc: list[float] = []
        self._clipped: int = 0

        self._current_sec_sum: float = 0.0
        self._current_sec_samples: int = 0
        self._sec_boundary: int = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not self._done and isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            if self._sample_rate == 0:
                self._init_params(frame)
            self._accumulate(frame)

        await self.push_frame(frame, direction)

    def _init_params(self, frame):
        self._sample_rate = getattr(frame, "sample_rate", 16000)
        self._num_channels = getattr(frame, "num_channels", 1)
        sw = 2
        self._window_bytes = int(WINDOW_SECS * self._sample_rate * self._num_channels * sw)
        self._sec_boundary = self._sample_rate * self._num_channels * sw
        logger.debug("[input_quality] armed — window={:.1f}s ({} bytes)",
                     WINDOW_SECS, self._window_bytes)

    def _accumulate(self, frame):
        data = frame.audio
        n_samples = len(data) // 2
        if n_samples == 0:
            return

        samples = struct.unpack(f"<{n_samples}h", data)

        sum_sq = 0.0
        for s in samples:
            sum_sq += s * s
            if abs(s) >= CLIP_THRESHOLD:
                self._clipped += 1
            self._current_sec_sum += s
            self._current_sec_samples += 1

        rms = (sum_sq / n_samples) ** 0.5
        self._frame_rms.append(rms)
        self._frame_count += 1
        self._bytes_seen += len(data)

        while self._current_sec_samples >= self._sample_rate:
            dc = self._current_sec_sum / self._sample_rate
            self._sec_dc.append(dc)
            self._current_sec_sum -= dc * self._sample_rate
            self._current_sec_samples -= self._sample_rate

        if self._bytes_seen >= self._window_bytes:
            self._emit_verdict()
            self._done = True

    def _emit_verdict(self):
        n = len(self._frame_rms)
        if n == 0:
            return

        sorted_rms = sorted(self._frame_rms)
        median_rms = sorted_rms[n // 2]
        pct_hot = sum(1 for r in self._frame_rms if r > RMS_HOT_THRESHOLD) / n

        dc_lo = min(self._sec_dc) if self._sec_dc else 0.0
        dc_hi = max(self._sec_dc) if self._sec_dc else 0.0
        dc_span = dc_hi - dc_lo

        problems = []
        if pct_hot >= PCT_HOT_WARN:
            problems.append(f"{100*pct_hot:.0f}% of frames RMS>{RMS_HOT_THRESHOLD} "
                            f"(median {median_rms:.0f})")
        if self._clipped >= CLIP_WARN:
            problems.append(f"{self._clipped} clipped samples")
        if dc_span >= DC_WANDER_WARN:
            problems.append(f"DC wander {dc_lo:+.0f} to {dc_hi:+.0f} "
                            f"(span {dc_span:.0f})")

        secs = self._bytes_seen / (self._sample_rate * self._num_channels * 2)

        if problems:
            logger.warning(
                "[input_quality] ⚠ DEGRADED signal over first {:.1f}s "
                "({} frames): {}. "
                "Possible causes: EMI from adjacent cables, excessive mic gain, "
                "or hardware fault. VAD may fail to detect speech transitions.",
                secs, n, "; ".join(problems),
            )
        else:
            logger.debug(
                "[input_quality] OK — {:.1f}s window, {} frames, "
                "median_rms={:.0f}, clipped={}, dc_span={:.0f}",
                secs, n, median_rms, self._clipped, dc_span,
            )
