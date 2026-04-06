"""
test_harness.py — EU-3d master-side harness.

Spawns the real recorder child (assistant/recorder_process.py), runs 3
wake->capture->VAD cycles reading ring buffer spans, then shuts down.

Usage (on Pi with ReSpeaker):
    cd ~/raspberry-ai/mvp-modules/forked_assistant
    source ~/pipecat-agent/venv/bin/activate
    python test/test_harness.py
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'assistant'))

from multiprocessing import Pipe, Process
from multiprocessing.shared_memory import SharedMemory

from recorder_process import recorder_child_entry
from audio_shm_ring import (
    SAMPLE_RATE, SAMPLE_WIDTH,
    SHM_NAME, SHM_SIZE,
    RingBufferReader,
)

TARGET_CYCLES = 3


# ---------------------------------------------------------------------------
# Master event loop
# ---------------------------------------------------------------------------

def master_loop(parent_conn, shm: SharedMemory, child: Process) -> None:
    """Synchronous master event loop. Runs TARGET_CYCLES wake->capture->VAD cycles."""
    ring_reader = RingBufferReader(shm)
    cycles = 0
    vad_start_pos = 0

    msg = parent_conn.recv()
    if msg["cmd"] != "READY":
        raise RuntimeError(f"Expected READY, got {msg}")
    print("[MASTER] child READY")

    parent_conn.send({"cmd": "SET_WAKE_LISTEN"})

    while cycles < TARGET_CYCLES:
        msg = parent_conn.recv()
        cmd = msg["cmd"]

        if cmd == "STATE_CHANGED":
            print(f"[MASTER] STATE_CHANGED -> {msg['state']}")

        elif cmd == "WAKE_DETECTED":
            print(
                f"[MASTER] WAKE_DETECTED  score={msg['score']:.3f}"
                f"  keyword={msg['keyword']}  write_pos={msg['write_pos']}"
            )
            parent_conn.send({"cmd": "SET_CAPTURE"})

        elif cmd == "VAD_STARTED":
            vad_start_pos = msg["write_pos"]
            print(f"[MASTER] VAD_STARTED    write_pos={vad_start_pos}")

        elif cmd == "VAD_STOPPED":
            end_pos = msg["write_pos"]
            audio = ring_reader.read(vad_start_pos, end_pos)
            duration = len(audio) / (SAMPLE_RATE * SAMPLE_WIDTH)
            print(
                f"[MASTER] VAD_STOPPED    write_pos={end_pos}"
                f"  captured {len(audio)} bytes ({duration:.2f} s)"
            )
            cycles += 1
            print(f"[MASTER] cycle {cycles}/{TARGET_CYCLES} complete")
            if cycles < TARGET_CYCLES:
                parent_conn.send({"cmd": "SET_WAKE_LISTEN"})

    print(f"[MASTER] {TARGET_CYCLES} cycles done — sending SHUTDOWN")
    parent_conn.send({"cmd": "SHUTDOWN"})
    child.join(timeout=5)
    if child.is_alive():
        print("[MASTER] child did not exit — terminating")
        child.terminate()
        child.join(timeout=2)
    if child.is_alive():
        print("[MASTER] child still alive — killing")
        child.kill()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        shm = SharedMemory(create=True, name=SHM_NAME, size=SHM_SIZE)
    except FileExistsError:
        old = SharedMemory(name=SHM_NAME, create=False)
        old.unlink()
        old.close()
        shm = SharedMemory(create=True, name=SHM_NAME, size=SHM_SIZE)

    parent_conn, child_conn = Pipe(duplex=True)

    child = Process(target=recorder_child_entry, args=(child_conn, SHM_NAME))
    child.start()
    child_conn.close()

    try:
        master_loop(parent_conn, shm, child)
    except KeyboardInterrupt:
        print("\n[MASTER] Ctrl+C — shutting down")
        try:
            parent_conn.send({"cmd": "SHUTDOWN"})
        except Exception:
            pass
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=2)
    finally:
        parent_conn.close()
        shm.unlink()
        shm.close()

    print("[MASTER] done")


if __name__ == "__main__":
    main()
