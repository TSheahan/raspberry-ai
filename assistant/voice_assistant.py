"""
voice_assistant.py — Master process for the forked assistant (EU-5/EU-6/EU-7: streaming + TTS).

Main entry point. Creates SharedMemory and Pipe, spawns the recorder child
on core 0, then runs a synchronous event loop that:

  1. Waits for READY from the recorder child
  2. Sends SET_WAKE_LISTEN
  3. On WAKE_DETECTED / STATE_CHANGED / VAD_* : delegated to WiredMasterState
     (agent prepare, SET_CAPTURE, Deepgram + ring tail on capture belief, cognitive
     path on VAD_STOPPED — see master_state_wired.py)
  4. On response complete: WiredMasterState sends SET_WAKE_LISTEN (after cognitive path)
  5. Handles Ctrl+C → SHUTDOWN sequence

STT: Deepgram Nova-3 live WebSocket (Mixed mode — Silero VAD is authoritative
dispatch trigger; Deepgram streams in parallel and accumulates is_final results).
Agent: CursorAgentSession (~/.local/bin/agent, stream-json, session continuity).
TTS: CartesiaTTS (primary), ElevenLabsTTS (fallback), DeepgramTTS (tertiary).
     Select via TTS_BACKEND env var; unset or empty → cartesia. See tts_backends.py for details.

Dependencies not in requirements.txt (already installed on Pi venv):
  deepgram-sdk, python-dotenv, cartesia, elevenlabs

Usage (on Pi with ReSpeaker):
    cd ~/raspberry-ai
    source ~/pipecat-agent/venv/bin/activate
    AGENT_WORKSPACE=~/raspberry-ai python assistant/voice_assistant.py
"""

import os
import sys
import time

from multiprocessing import Pipe, Process
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

from deepgram import DeepgramClient

from logging_setup import configure_logging
from agent_session import CursorAgentSession
from master_state_wired import WiredMasterState
from recorder_process import recorder_child_entry
from tts_backends import TTSBackend, CartesiaTTS, ElevenLabsTTS, DeepgramTTS
from audio_shm_ring import SHM_NAME, SHM_SIZE, AudioShmRingReader

logger = logger.bind(name="master")

# --- Agent / session configuration (override via environment) ---
_AGENT_WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", str(Path.home() / "raspberry-ai")))
_AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-4.6-sonnet-medium")
_AGENT_BIN = Path(os.environ.get("AGENT_BIN", str(Path.home() / ".local/bin/agent")))

# --- TTS configuration (override via environment) ---
# TTS_BACKEND: cartesia (default/primary), elevenlabs (fallback), deepgram (tertiary)
# Use ``or`` so unset *or* empty string (common in .env placeholders) → cartesia.
_TTS_BACKEND = (os.environ.get("TTS_BACKEND") or "cartesia").strip().lower()
_TTS_BACKENDS: dict[str, type[TTSBackend]] = {
    "cartesia": CartesiaTTS,
    "elevenlabs": ElevenLabsTTS,
    "deepgram": DeepgramTTS,
}

# ---------------------------------------------------------------------------
# Child shutdown sequence (spec §3)
# ---------------------------------------------------------------------------

def shutdown_child(pipe, child: Process) -> None:
    """Wait for child to complete safe shutdown, escalating if necessary.

    Sends SHUTDOWN command (idempotent — child may already be tearing down
    from its own SIGINT). Then drains the pipe looking for SHUTDOWN_FINISHED.
    If the child doesn't finish within the deadline, escalates to SIGTERM,
    then SIGKILL.
    """
    try:
        pipe.send({"cmd": "SHUTDOWN"})
    except Exception:
        pass

    deadline = time.time() + 5
    finished = False
    while time.time() < deadline and child.is_alive():
        try:
            if pipe.poll(0.2):
                msg = pipe.recv()
                cmd = msg.get("cmd")
                if cmd == "SHUTDOWN_COMMENCED":
                    logger.info("[master] child: shutdown commenced")
                elif cmd == "SHUTDOWN_FINISHED":
                    logger.info("[master] child: shutdown finished")
                    finished = True
                    break
        except (EOFError, OSError):
            break

    child.join(timeout=2)
    if child.is_alive():
        if not finished:
            logger.warning("[master] no SHUTDOWN_FINISHED — terminating child")
        else:
            logger.warning("[master] child did not exit after SHUTDOWN_FINISHED — terminating")
        child.terminate()
        child.join(timeout=2)
    if child.is_alive():
        logger.error("[master] child still alive — killing")
        child.kill()


# ---------------------------------------------------------------------------
# Master event loop
# ---------------------------------------------------------------------------

def master_loop(pipe, shm: SharedMemory, child: Process) -> None:
    ring_reader = AudioShmRingReader(shm)
    dg_client = DeepgramClient()
    agent = CursorAgentSession(
        workspace=_AGENT_WORKSPACE,
        model=_AGENT_MODEL,
        agent_bin=_AGENT_BIN,
    )
    if _TTS_BACKEND not in _TTS_BACKENDS:
        raise RuntimeError(f"Unknown TTS_BACKEND={_TTS_BACKEND!r}; choose from {list(_TTS_BACKENDS)}")
    tts = _TTS_BACKENDS[_TTS_BACKEND]()
    state = WiredMasterState()
    state.set_pipe(pipe)
    state.set_agent(agent)
    state.set_tts(tts)
    state.set_ring_reader(ring_reader)
    state.set_dg_client(dg_client)

    msg = pipe.recv()
    if msg["cmd"] != "READY":
        raise RuntimeError(f"Expected READY, got {msg}")
    logger.info("[master] recorder child READY")

    pipe.send({"cmd": "SET_WAKE_LISTEN"})
    logger.info("listening for wake word...")

    try:
        while True:
            msg = pipe.recv()
            cmd = msg["cmd"]

            if cmd == "STATE_CHANGED":
                state.on_state_changed(msg["state"])

            elif cmd == "WAKE_DETECTED":
                if not state.on_wake_detected(
                    msg["write_pos"], msg["score"], msg["keyword"]
                ):
                    logger.warning(
                        "[master] WAKE_DETECTED ignored (processing={} phase={})",
                        state.processing,
                        state.phase,
                    )

            elif cmd == "VAD_STARTED":
                if state.on_vad_started(msg["write_pos"]):
                    logger.info("[master] VAD_STARTED    write_pos={}", msg["write_pos"])

            elif cmd == "VAD_STOPPED":
                state.on_vad_stopped(msg["write_pos"])

            elif cmd == "SHUTDOWN_COMMENCED":
                logger.info("[master] child initiated shutdown")
                return

            elif cmd == "SHUTDOWN_FINISHED":
                # Child exited cleanly without a prior SHUTDOWN_COMMENCED
                # (e.g. retroactive send from unexpected pipeline exit).
                # Treat as a clean child-initiated shutdown rather than a crash.
                logger.info("[master] child finished without SHUTDOWN_COMMENCED — exiting cleanly")
                return

            elif cmd == "ERROR":
                logger.error("[master] ERROR from child: {}", msg.get("msg", "?"))

    finally:
        state.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    configure_logging()

    if not os.environ.get("DEEPGRAM_API_KEY"):
        sys.exit("DEEPGRAM_API_KEY not set. Export it or add to ~/.env")

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

    logger.info("[master] recorder child spawned (pid={})", child.pid)

    try:
        master_loop(parent_conn, shm, child)
    except KeyboardInterrupt:
        logger.info("[master] Ctrl+C — shutting down")
    except EOFError:
        logger.error("[master] pipe broken — recorder child likely crashed")
    finally:
        shutdown_child(parent_conn, child)
        parent_conn.close()
        try:
            shm.unlink()
        except FileNotFoundError:
            pass
        shm.close()

    logger.info("[master] done")


if __name__ == "__main__":
    main()
