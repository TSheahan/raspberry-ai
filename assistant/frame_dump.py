"""
frame_dump.py — Inline diagnostic tap that writes raw PCM from the Pipecat
pipeline to disk.

Activated by PIPELINE_FRAME_DUMP=1.  Captures audio exactly as it leaves
the input transport, before VAD / OWW / ring writer see it.  Output files:

    ~/pipeline_dump_<YYYYMMDD_HHMMSS>.pcm   — raw int16 PCM bytes
    ~/pipeline_dump_<YYYYMMDD_HHMMSS>.meta   — text sidecar with format info
"""

import os
import time
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    Frame, AudioRawFrame, InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

MAX_DUMP_SECS = int(os.environ.get("FRAME_DUMP_MAX_SECS", "30"))


def frame_dump_enabled() -> bool:
    return os.environ.get("PIPELINE_FRAME_DUMP", "0") == "1"


class FrameDumpProcessor(FrameProcessor):
    """Write every audio frame's raw bytes to a PCM file on disk.

    Read-only tap: never alters, drops, or delays any frame.
    """

    def __init__(self, prefix: str = "pipeline_dump"):
        super().__init__()
        self._prefix = prefix
        self._pcm_file = None
        self._meta_written = False
        self._bytes_written: int = 0
        self._frame_count: int = 0
        self._capped = False
        self._sample_rate: int = 0
        self._num_channels: int = 0
        self._max_bytes: int = 0
        self._pcm_path: str = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            if self._pcm_file is None and not self._capped:
                self._open_files(frame)
            if self._pcm_file is not None and not self._capped:
                self._write(frame)

        await self.push_frame(frame, direction)

    def _open_files(self, frame):
        sr = getattr(frame, "sample_rate", 16000)
        ch = getattr(frame, "num_channels", 1)
        sw = 2  # int16
        self._sample_rate = sr
        self._num_channels = ch
        self._max_bytes = MAX_DUMP_SECS * sr * ch * sw

        ts = time.strftime("%Y%m%d_%H%M%S")
        stem = Path.home() / f"{self._prefix}_{ts}"
        self._pcm_path = str(stem.with_suffix(".pcm"))
        meta_path = str(stem.with_suffix(".meta"))

        self._pcm_file = open(self._pcm_path, "wb")

        with open(meta_path, "w") as mf:
            mf.write(f"sample_rate={sr}\n")
            mf.write(f"channels={ch}\n")
            mf.write(f"sample_width={sw}\n")
            mf.write("format=int16le\n")

        logger.info("[frame_dump] opened {} — sr={} ch={} audio_len={}",
                    self._pcm_path, sr, ch, len(frame.audio))

    def _write(self, frame):
        data = frame.audio
        self._pcm_file.write(data)
        self._bytes_written += len(data)
        self._frame_count += 1

        if self._frame_count == 1 or self._frame_count % 50 == 0:
            logger.log("TRACE", "[frame_dump] frame={} bytes={}",
                       self._frame_count, self._bytes_written)

        if self._bytes_written >= self._max_bytes:
            secs = self._bytes_written / (self._sample_rate * self._num_channels * 2)
            logger.warning("[frame_dump] cap reached: {} bytes ({:.1f}s) — writes stopped",
                           self._bytes_written, secs)
            self._capped = True
            self._close()

    def _close(self):
        if self._pcm_file is not None:
            self._pcm_file.close()
            logger.info("[frame_dump] closed {} — {} bytes total",
                        self._pcm_path, self._bytes_written)
            self._pcm_file = None

    async def cleanup(self):
        await super().cleanup()
        self._close()
