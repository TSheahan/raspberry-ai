"""
EU-1 + EU-2: SharedMemory and Ring Buffer Smoke Tests

EU-1 — Proves SharedMemory works across a fork on ARM64 Linux with this
        Python build, and that aligned uint64 reads are coherent without locks.

EU-2 — Proves ring_buffer.RingBufferWriter / RingBufferReader work correctly:
        IPC test (fork): child writes audio-sized frames, master reads span.
        Wrap test (in-process): write past ring boundary, verify wrap-around
        and stale detection.
"""

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import struct
import time
from multiprocessing import Process
from multiprocessing.shared_memory import SharedMemory

from ring_buffer import (
    FRAME_BYTES, RING_SIZE, SHM_SIZE, SAMPLE_RATE, SAMPLE_WIDTH,
    RingBufferWriter, RingBufferReader, read_header,
)

# ---------------------------------------------------------------------------
# EU-1
# ---------------------------------------------------------------------------

_EU1_SHM_NAME   = "eu1_smoke"
_EU1_SHM_SIZE   = 16        # 2 × uint64
_COUNTER_OFFSET = 0
_DONE_OFFSET    = 8
_N_WRITES       = 10
_WRITE_INTERVAL = 0.05


def _eu1_child(shm_name: str) -> None:
    shm = SharedMemory(name=shm_name, create=False)
    try:
        for i in range(1, _N_WRITES + 1):
            struct.pack_into("Q", shm.buf, _COUNTER_OFFSET, i)
            time.sleep(_WRITE_INTERVAL)
        struct.pack_into("Q", shm.buf, _DONE_OFFSET, 1)
    finally:
        shm.close()


def test_eu1() -> None:
    shm = SharedMemory(name=_EU1_SHM_NAME, create=True, size=_EU1_SHM_SIZE)
    struct.pack_into("QQ", shm.buf, 0, 0, 0)

    child = Process(target=_eu1_child, args=(_EU1_SHM_NAME,), daemon=True)
    child.start()
    print(f"[EU-1] child pid={child.pid}")

    last = 0
    while True:
        counter = struct.unpack_from("Q", shm.buf, _COUNTER_OFFSET)[0]
        done    = struct.unpack_from("Q", shm.buf, _DONE_OFFSET)[0]
        if counter != last:
            print(f"[EU-1] counter={counter}")
            last = counter
        if done:
            break
        time.sleep(0.01)

    child.join(timeout=2.0)
    if child.is_alive():
        child.terminate()
        child.join()
        raise RuntimeError("EU-1: child did not exit")

    shm.close()
    shm.unlink()
    print(f"[EU-1] child exitcode={child.exitcode} — PASS")


# ---------------------------------------------------------------------------
# EU-2a: IPC test — fork writes audio frames, master reads span
# ---------------------------------------------------------------------------

_EU2_SHM_NAME = "eu2_ring"
_N_FRAMES     = 50           # frames to write (~1 second of audio)


def _eu2_child(shm_name: str) -> None:
    shm = SharedMemory(name=shm_name, create=False)
    writer = RingBufferWriter(shm)
    try:
        for i in range(_N_FRAMES):
            # Each frame: byte value = i % 256, repeated FRAME_BYTES times
            writer.write(bytes([i % 256]) * FRAME_BYTES)
    finally:
        shm.close()


def test_eu2_ipc() -> None:
    shm = SharedMemory(name=_EU2_SHM_NAME, create=True, size=SHM_SIZE)

    child = Process(target=_eu2_child, args=(_EU2_SHM_NAME,), daemon=True)
    child.start()
    print(f"[EU-2a] child pid={child.pid}")

    reader    = RingBufferReader(shm)
    expected  = _N_FRAMES * FRAME_BYTES
    deadline  = time.monotonic() + 5.0
    while reader.write_pos < expected:
        if time.monotonic() > deadline:
            raise RuntimeError(f"EU-2a: timeout waiting for frames (write_pos={reader.write_pos})")
        time.sleep(0.005)

    child.join(timeout=2.0)
    if child.is_alive():
        child.terminate()
        child.join()
        raise RuntimeError("EU-2a: child did not exit")

    # Read the full span and verify
    hdr   = read_header(shm)
    audio = reader.read(0, hdr["write_pos"])
    n_bytes    = len(audio)
    duration_s = n_bytes / (SAMPLE_RATE * SAMPLE_WIDTH)

    # Spot-check frame content: first byte of each frame should be i % 256
    for i in range(_N_FRAMES):
        got      = audio[i * FRAME_BYTES]
        expected_byte = i % 256
        if got != expected_byte:
            raise RuntimeError(f"EU-2a: frame {i} byte0={got}, expected {expected_byte}")

    shm.close()
    shm.unlink()
    print(f"[EU-2a] bytes={n_bytes}  duration={duration_s:.3f}s  exitcode={child.exitcode} — PASS")


# ---------------------------------------------------------------------------
# EU-2b: Wrap test — in-process, write past ring boundary
# ---------------------------------------------------------------------------

_EU2B_SHM_NAME = "eu2_wrap"


def test_eu2_wrap() -> None:
    shm    = SharedMemory(name=_EU2B_SHM_NAME, create=True, size=SHM_SIZE)
    writer = RingBufferWriter(shm)
    reader = RingBufferReader(shm)

    # Write enough frames to wrap the ring: RING_SIZE // FRAME_BYTES + 2
    frames_to_fill = RING_SIZE // FRAME_BYTES + 2
    for i in range(frames_to_fill):
        writer.write(bytes([i % 256]) * FRAME_BYTES)

    # Start of ring is now stale
    assert reader.is_stale(0), "EU-2b: expected pos=0 to be stale after wrap"

    # Most-recent frame is NOT stale
    last_frame_start = writer.write_pos - FRAME_BYTES
    assert not reader.is_stale(last_frame_start), "EU-2b: last frame should not be stale"

    # Read the last frame and verify content
    last_frame = reader.read(last_frame_start, writer.write_pos)
    expected_byte = (frames_to_fill - 1) % 256
    assert len(last_frame) == FRAME_BYTES, f"EU-2b: expected {FRAME_BYTES} bytes, got {len(last_frame)}"
    assert last_frame[0] == expected_byte, f"EU-2b: byte0={last_frame[0]}, expected {expected_byte}"

    shm.close()
    shm.unlink()
    print(f"[EU-2b] wrap after {frames_to_fill} frames, stale detection OK, last-frame read OK — PASS")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_eu1()
    test_eu2_ipc()
    test_eu2_wrap()
    print("\nAll smoke tests passed.")
