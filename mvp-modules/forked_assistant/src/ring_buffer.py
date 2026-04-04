"""
ring_buffer.py — Shared-memory ring buffer for recorder→master audio transfer.

Memory layout (HEADER_SIZE + RING_SIZE bytes):

  HEADER (64 bytes):
    offset  0: write_pos    <Q   monotonically increasing byte offset
    offset  8: sample_rate  <Q   Hz (16000)
    offset 16: channels     <H   1
    offset 18: sample_width <H   bytes per sample (2 = int16)
    offset 20: frame_size   <I   bytes per frame (640)
    offset 24: reserved          40 bytes, zero

  RING (RING_SIZE bytes at offset HEADER_SIZE):
    Circular buffer of raw int16 PCM data, single-writer / single-reader.
    write_pos advances monotonically; ring index = write_pos % RING_SIZE.

Write protocol: data is written before write_pos is advanced.
Read protocol:  read write_pos first; data is valid if (write_pos - start) <= RING_SIZE.
"""
import struct
from multiprocessing.shared_memory import SharedMemory

from log_config import TRACE
from loguru import logger

# --- Layout ---
HEADER_SIZE = 64
RING_SIZE   = 524288        # 512 KB ≈ 16.4 s at 16 kHz int16 mono
SHM_SIZE    = HEADER_SIZE + RING_SIZE
SHM_NAME    = "recorder_audio"

# --- Audio format (ReSpeaker hat, fixed) ---
SAMPLE_RATE    = 16000
CHANNELS       = 1
SAMPLE_WIDTH   = 2          # bytes (int16)
FRAME_DURATION = 0.020      # seconds
FRAME_SAMPLES  = 320
FRAME_BYTES    = 640        # bytes per 20 ms frame

# Header field offsets
_WP = 0   # write_pos    <Q
_SR = 8   # sample_rate  <Q
_CH = 16  # channels     <H
_SW = 18  # sample_width <H
_FS = 20  # frame_size   <I


# ---------------------------------------------------------------------------
# Header utilities
# ---------------------------------------------------------------------------

def init_header(shm: SharedMemory) -> None:
    """Zero header, then write fixed audio-format fields. Called once by writer."""
    shm.buf[:HEADER_SIZE] = bytes(HEADER_SIZE)
    struct.pack_into('<Q', shm.buf, _SR, SAMPLE_RATE)
    struct.pack_into('<H', shm.buf, _CH, CHANNELS)
    struct.pack_into('<H', shm.buf, _SW, SAMPLE_WIDTH)
    struct.pack_into('<I', shm.buf, _FS, FRAME_BYTES)
    logger.debug("header initialized: sr={} ch={} sw={} frame={} bytes",
                 SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, FRAME_BYTES)


def read_header(shm: SharedMemory) -> dict:
    """Return all header fields as a dict."""
    return {
        "write_pos":    struct.unpack_from('<Q', shm.buf, _WP)[0],
        "sample_rate":  struct.unpack_from('<Q', shm.buf, _SR)[0],
        "channels":     struct.unpack_from('<H', shm.buf, _CH)[0],
        "sample_width": struct.unpack_from('<H', shm.buf, _SW)[0],
        "frame_size":   struct.unpack_from('<I', shm.buf, _FS)[0],
    }


# ---------------------------------------------------------------------------
# Writer (recorder child)
# ---------------------------------------------------------------------------

class RingBufferWriter:
    """Write audio frames into the ring buffer. Single writer only."""

    def __init__(self, shm: SharedMemory) -> None:
        self._shm = shm
        self._write_pos = 0
        init_header(shm)

    @property
    def write_pos(self) -> int:
        return self._write_pos

    def write(self, frame_bytes: bytes) -> None:
        """Write frame_bytes into the ring, then advance write_pos."""
        n = len(frame_bytes)
        offset = self._write_pos % RING_SIZE
        base = HEADER_SIZE
        if offset + n <= RING_SIZE:
            self._shm.buf[base + offset : base + offset + n] = frame_bytes
        else:
            split = RING_SIZE - offset
            self._shm.buf[base + offset : base + RING_SIZE] = frame_bytes[:split]
            self._shm.buf[base : base + (n - split)]        = frame_bytes[split:]
        # Advance after data is in place (ARM64: aligned Q store is atomic)
        self._write_pos += n
        struct.pack_into('<Q', self._shm.buf, _WP, self._write_pos)
        logger.log(TRACE, "write {} bytes at pos {}", n, self._write_pos)


# ---------------------------------------------------------------------------
# Reader (master)
# ---------------------------------------------------------------------------

class RingBufferReader:
    """Read spans of audio from the ring buffer. Single reader."""

    def __init__(self, shm: SharedMemory) -> None:
        self._shm = shm

    @property
    def write_pos(self) -> int:
        return struct.unpack_from('<Q', self._shm.buf, _WP)[0]

    def is_stale(self, pos: int) -> bool:
        """True if the data at pos has been overwritten by the writer."""
        return (self.write_pos - pos) > RING_SIZE

    def read(self, start_pos: int, end_pos: int) -> bytes:
        """Return audio bytes between start_pos and end_pos.

        Returns b'' if the span is empty, out-of-range, or stale.
        """
        length = end_pos - start_pos
        if length <= 0 or length > RING_SIZE:
            return b''
        if self.is_stale(start_pos):
            return b''
        base   = HEADER_SIZE
        offset = start_pos % RING_SIZE
        logger.log(TRACE, "read {} bytes [{}:{}]", length, start_pos, end_pos)
        if offset + length <= RING_SIZE:
            return bytes(self._shm.buf[base + offset : base + offset + length])
        split  = RING_SIZE - offset
        part1  = bytes(self._shm.buf[base + offset : base + RING_SIZE])
        part2  = bytes(self._shm.buf[base : base + (length - split)])
        return part1 + part2
