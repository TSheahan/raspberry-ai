"""
track1_ipc_harness.py — EU-3b: IPC/Buffer harness (Track 1).

Proves the process boundary: SharedMemory, Pipe, fork, core pinning, shutdown.
No Pipecat. No PyAudio. No ONNX.

Two components:
  - RecorderTrack1: RecorderState subclass with real downstream port (ring
    writes, pipe sends) and no-op stream/model ops.
  - FakeAudioDriver: coroutine that simulates PyAudio frame production and
    OWW/Silero events at real 20 ms cadence.

Run directly:
  python track1_ipc_harness.py

Completes 3 full wake→capture→VAD cycles, then sends SHUTDOWN.
Test Ctrl+C at any point — shutdown must be clean (no hang, no reboot).
"""

import asyncio
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from multiprocessing import Pipe, Process
from multiprocessing.shared_memory import SharedMemory

from recorder_state import RecorderState
from ring_buffer import (
    FRAME_BYTES, FRAME_DURATION, SAMPLE_RATE, SAMPLE_WIDTH,
    SHM_NAME, SHM_SIZE,
    RingBufferReader, RingBufferWriter,
)


# ---------------------------------------------------------------------------
# RecorderTrack1 — real downstream port, no-op stream/model ops
# ---------------------------------------------------------------------------

class RecorderTrack1(RecorderState):
    """RecorderState with real ring-buffer writes and real pipe signals.

    Stream lifecycle (_start_stream, _stop_stream) and model resets
    (_reset_oww_full, _clear_oww, _reset_silero) are no-ops — Track 1
    has no PyAudio or ONNX.
    """

    def __init__(self, pipe, ring_writer: RingBufferWriter):
        # shm strong-ref not needed here; RingBufferWriter owns it
        super().__init__(pipe=pipe, shm=None)
        self._ring_writer = ring_writer

    # --- Audio write ---

    def write_audio(self, frame_bytes: bytes) -> None:
        self._ring_writer.write(frame_bytes)
        self._write_pos = self._ring_writer.write_pos

    # --- Signal emission (→ pipe) ---

    def signal_wake_detected(self, score: float, keyword: str) -> None:
        self._pipe.send({
            "cmd": "WAKE_DETECTED",
            "write_pos": self._write_pos,
            "score": score,
            "keyword": keyword,
        })

    def signal_vad_started(self) -> None:
        self._pipe.send({"cmd": "VAD_STARTED", "write_pos": self._write_pos})

    def signal_vad_stopped(self) -> None:
        self._pipe.send({"cmd": "VAD_STOPPED", "write_pos": self._write_pos})

    def signal_state_changed(self) -> None:
        self._pipe.send({"cmd": "STATE_CHANGED", "state": self._phase})

    # --- Stream lifecycle — no-op ---

    async def _start_stream(self) -> None:
        pass

    async def _stop_stream(self) -> None:
        pass

    # --- Model resets — no-op ---

    def _reset_oww_full(self) -> None:
        pass

    def _clear_oww(self) -> None:
        pass

    async def _reset_silero(self) -> None:
        pass


# ---------------------------------------------------------------------------
# FakeAudioDriver — simulates PyAudio + OWW/Silero events
# ---------------------------------------------------------------------------

async def fake_audio_driver(
    state: RecorderTrack1,
    wake_delay_secs: float = 4.0,
    speech_duration_secs: float = 2.5,
    silence_after_secs: float = 1.0,
) -> None:
    """Emit frames at real 20 ms cadence and fire wake/VAD signals on timers.

    Does NOT call set_phase() — that is the command listener's responsibility.
    Runs continuously; each utterance cycle repeats after completion.
    """
    frame = bytes(FRAME_BYTES)  # 640 zero-bytes per 20 ms frame
    loop = asyncio.get_event_loop()

    while True:
        # WAKE_LISTEN phase: emit frames, fire wake word after delay
        wake_deadline = loop.time() + wake_delay_secs
        while loop.time() < wake_deadline:
            if state.wake_listen:
                state.write_audio(frame)
                state.inc_total_frames()
            await asyncio.sleep(FRAME_DURATION)

        if state.wake_listen:
            state.signal_wake_detected(score=0.75, keyword="hey_jarvis")

        # Wait for command listener to transition to CAPTURE
        for _ in range(50):  # up to 1 s
            if state.capture:
                break
            await asyncio.sleep(0.020)

        # CAPTURE phase: emit frames, fire VAD_STARTED → frames → VAD_STOPPED
        if state.capture:
            state.signal_vad_started()
            speech_deadline = loop.time() + speech_duration_secs
            while loop.time() < speech_deadline:
                state.write_audio(frame)
                state.inc_total_frames()
                state.inc_vad_frames()
                await asyncio.sleep(FRAME_DURATION)

            await asyncio.sleep(silence_after_secs)
            state.signal_vad_stopped()

        # Wait for command listener to transition back to WAKE_LISTEN
        for _ in range(50):
            if state.wake_listen:
                break
            await asyncio.sleep(0.020)


# ---------------------------------------------------------------------------
# command_listener — routes pipe commands to state.set_phase()
# ---------------------------------------------------------------------------

async def command_listener(state: RecorderTrack1, pipe) -> None:
    """Poll pipe for master commands; call set_phase() on each.

    Returns when SHUTDOWN is received.
    """
    while True:
        if pipe.poll(0):  # non-blocking check
            msg = pipe.recv()
            cmd = msg.get("cmd")
            if cmd == "SET_WAKE_LISTEN":
                await state.set_phase("wake_listen")
            elif cmd == "SET_CAPTURE":
                await state.set_phase("capture")
            elif cmd == "SET_DORMANT":
                await state.set_phase("dormant")
            elif cmd == "SHUTDOWN":
                return
        await asyncio.sleep(0.010)


# ---------------------------------------------------------------------------
# Child process
# ---------------------------------------------------------------------------

async def recorder_child_main(pipe, shm_name: str) -> None:
    shm = SharedMemory(name=shm_name, create=False)
    try:
        ring_writer = RingBufferWriter(shm)
        state = RecorderTrack1(pipe=pipe, ring_writer=ring_writer)

        pipe.send({"cmd": "READY"})

        driver_task = asyncio.create_task(fake_audio_driver(state))
        listener_task = asyncio.create_task(command_listener(state, pipe))

        await listener_task          # returns on SHUTDOWN
        driver_task.cancel()
        try:
            await driver_task
        except asyncio.CancelledError:
            pass
    finally:
        shm.close()


def recorder_child_entry(pipe, shm_name: str) -> None:
    """multiprocessing.Process target. Pins to core 0, then runs async main."""
    os.sched_setaffinity(0, {0})
    asyncio.run(recorder_child_main(pipe, shm_name))


# ---------------------------------------------------------------------------
# Master
# ---------------------------------------------------------------------------

def master_loop(parent_conn, shm: SharedMemory, child: Process) -> None:
    """Synchronous master event loop. Runs 3 wake→capture→VAD cycles."""
    ring_reader = RingBufferReader(shm)
    cycles = 0
    vad_start_pos = 0

    # Step 4: wait for READY
    msg = parent_conn.recv()
    if msg["cmd"] != "READY":
        raise RuntimeError(f"Expected READY, got {msg}")
    print("[MASTER] child READY")

    # Step 5: arm wake word detection
    parent_conn.send({"cmd": "SET_WAKE_LISTEN"})

    while cycles < 3:
        msg = parent_conn.recv()
        cmd = msg["cmd"]

        if cmd == "STATE_CHANGED":
            print(f"[MASTER] STATE_CHANGED → {msg['state']}")

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
            print(f"[MASTER] cycle {cycles}/3 complete")
            if cycles < 3:
                parent_conn.send({"cmd": "SET_WAKE_LISTEN"})

    # Steps 10–11: shutdown
    print("[MASTER] 3 cycles done — sending SHUTDOWN")
    parent_conn.send({"cmd": "SHUTDOWN"})
    child.join(timeout=3)
    if child.is_alive():
        print("[MASTER] child did not exit — terminating")
        child.terminate()
        child.join(timeout=2)
    if child.is_alive():
        print("[MASTER] child still alive — killing")
        child.kill()


def main() -> None:
    # Step 1: create SharedMemory
    try:
        shm = SharedMemory(create=True, name=SHM_NAME, size=SHM_SIZE)
    except FileExistsError:
        # Leftover from a previous run; unlink and recreate
        old = SharedMemory(name=SHM_NAME, create=False)
        old.unlink()
        old.close()
        shm = SharedMemory(create=True, name=SHM_NAME, size=SHM_SIZE)

    # Step 2: create Pipe
    parent_conn, child_conn = Pipe(duplex=True)

    # Step 3: spawn child
    child = Process(target=recorder_child_entry, args=(child_conn, SHM_NAME))
    child.start()
    child_conn.close()  # master does not need child end

    try:
        master_loop(parent_conn, shm, child)
    except KeyboardInterrupt:
        print("\n[MASTER] Ctrl+C — shutting down")
        try:
            parent_conn.send({"cmd": "SHUTDOWN"})
        except Exception:
            pass
        child.join(timeout=3)
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
