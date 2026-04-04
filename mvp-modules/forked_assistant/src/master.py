"""
master.py — Master process for the forked assistant (EU-5/EU-6: streaming).

Main entry point. Creates SharedMemory and Pipe, spawns the recorder child
on core 0, then runs a synchronous event loop that:

  1. Waits for READY from the recorder child
  2. Sends SET_WAKE_LISTEN
  3. On WAKE_DETECTED: pre-spawns agent subprocess (EU-6) + opens Deepgram
     live WebSocket + starts ring-tail thread (EU-5) concurrently; sends SET_CAPTURE
  4. On VAD_STOPPED: stops ring-tail, finalizes Deepgram stream, assembles
     transcript, calls agent.run() for streaming text output
  5. On response complete: sends SET_WAKE_LISTEN
  6. Handles Ctrl+C → SHUTDOWN sequence

STT: Deepgram Nova-3 live WebSocket (Mixed mode — Silero VAD is authoritative
dispatch trigger; Deepgram streams in parallel and accumulates is_final results).
Agent: CursorAgentSession (~/.local/bin/agent, stream-json, session continuity).

Dependencies not in requirements.txt (already installed on Pi venv):
  deepgram-sdk, python-dotenv

Usage (on Pi with ReSpeaker):
    cd ~/raspberry-ai/mvp-modules/forked_assistant
    source ~/pipecat-agent/venv/bin/activate
    AGENT_WORKSPACE=~/raspberry-ai python src/master.py
"""

import logging
import os
import sys
import threading
import time

from multiprocessing import Pipe, Process
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from deepgram import DeepgramClient
from deepgram.core.events import EventType

from log_config import configure_logging
from agent_session import AgentSession, CursorAgentSession, AgentError
from recorder_child import recorder_child_entry
from ring_buffer import (
    CHANNELS, SAMPLE_RATE,
    SHM_NAME, SHM_SIZE,
    RingBufferReader,
)

logger = logging.getLogger("master")

# --- Agent / session configuration (override via environment) ---
_AGENT_WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", str(Path.home() / "raspberry-ai")))
_AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-4.6-sonnet-medium")
_AGENT_BIN = Path(os.environ.get("AGENT_BIN", str(Path.home() / ".local/bin/agent")))

# Deepgram keepalive: send every N seconds when ring write_pos has not advanced.
# Deepgram closes the WebSocket with NET-0001 after 10 s of silence.
_DG_KEEPALIVE_INTERVAL = 3.5


# ---------------------------------------------------------------------------
# Streaming capture session — ring tail + Deepgram live WebSocket
# ---------------------------------------------------------------------------

class _CaptureSession:
    """State for one WAKE_DETECTED → VAD_STOPPED capture window."""

    def __init__(self) -> None:
        self._transcripts: list[str] = []
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def add_transcript(self, text: str) -> None:
        with self._lock:
            self._transcripts.append(text)

    def get_transcript(self) -> str:
        with self._lock:
            return " ".join(self._transcripts)


def _run_capture(
    capture: _CaptureSession,
    ring_reader: RingBufferReader,
    wake_pos: int,
    dg_client: DeepgramClient,
) -> None:
    """Ring-tail + Deepgram live session. Runs in a thread from WAKE_DETECTED to VAD_STOPPED.

    Opens a Deepgram live WebSocket, tails the ring buffer at ~20 ms intervals,
    and accumulates is_final transcripts in capture.  On stop_event, flushes
    remaining audio, sends send_finalize(), and closes the connection.
    """

    def on_message(message) -> None:
        if getattr(message, "type", None) != "Results":
            return
        if not message.is_final:
            return
        try:
            text = message.channel.alternatives[0].transcript.strip()
            if text:
                capture.add_transcript(text)
                logger.debug("[dg-live] is_final: %r", text)
        except Exception as exc:
            logger.debug("[dg-live] on_message parse error: %s", exc)

    def on_error(error) -> None:
        logger.error("[dg-live] error: %s", error)

    try:
        with dg_client.listen.v1.connect(
            model="nova-3",
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            language="en-US",
            smart_format=True,
            interim_results=True,
            endpointing=300,
        ) as conn:
            conn.on(EventType.MESSAGE, on_message)
            conn.on(EventType.ERROR, on_error)
            listen_thread = threading.Thread(target=conn.start_listening, daemon=True)
            listen_thread.start()

            pos = wake_pos
            last_keepalive = time.monotonic()

            while not capture.stop_event.is_set():
                new_wp = ring_reader.write_pos
                if new_wp != pos:
                    chunk = ring_reader.read(pos, new_wp)
                    if chunk:
                        conn.send_media(chunk)
                        last_keepalive = time.monotonic()
                    pos = new_wp
                elif time.monotonic() - last_keepalive >= _DG_KEEPALIVE_INTERVAL:
                    conn.send_keep_alive()
                    last_keepalive = time.monotonic()
                    logger.debug("[dg-live] keepalive sent")
                time.sleep(0.02)

            # Flush any frames written between the last poll and stop_event.
            new_wp = ring_reader.write_pos
            if new_wp != pos:
                chunk = ring_reader.read(pos, new_wp)
                if chunk:
                    conn.send_media(chunk)

            conn.send_finalize()
            time.sleep(0.2)
            conn.send_close_stream()
            listen_thread.join(timeout=2)

    except Exception as exc:
        logger.error("[dg-live] capture session error: %s", exc)


# ---------------------------------------------------------------------------
# Cognitive loop — agent response (EU-6)
# ---------------------------------------------------------------------------

def cognitive_loop(transcript: str, agent: AgentSession) -> None:
    """Feed transcript to agent; print streaming text chunks as they arrive."""
    if not transcript:
        logger.warning("no transcript — skipping cognitive loop")
        return
    logger.info("TRANSCRIPT: %s", transcript)
    loop_start = time.monotonic()
    try:
        for text_chunk in agent.run(transcript):
            print(text_chunk, end="", flush=True)
        print()
    except AgentError as exc:
        logger.error("[agent] error: %s", exc)
    logger.info("cognitive loop latency: %.2fs", time.monotonic() - loop_start)


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
    ring_reader = RingBufferReader(shm)
    dg_client = DeepgramClient()
    agent = CursorAgentSession(
        workspace=_AGENT_WORKSPACE,
        model=_AGENT_MODEL,
        agent_bin=_AGENT_BIN,
    )
    processing = False
    wake_pos = 0
    capture: _CaptureSession | None = None

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
                logger.debug("[master] state -> %s", msg["state"])

            elif cmd == "WAKE_DETECTED":
                if processing:
                    logger.warning("[master] still processing previous utterance, ignoring wake")
                    continue
                wake_pos = msg["write_pos"]
                logger.info("[master] WAKE_DETECTED  score=%.3f  keyword=%s",
                            msg["score"], msg["keyword"])

                # Pre-spawn agent (hides startup latency behind the STT window)
                # and open Deepgram live session + ring tail concurrently.
                agent.prepare()
                capture = _CaptureSession()
                capture.thread = threading.Thread(
                    target=_run_capture,
                    args=(capture, ring_reader, wake_pos, dg_client),
                    daemon=True,
                )
                capture.thread.start()
                pipe.send({"cmd": "SET_CAPTURE"})

            elif cmd == "VAD_STARTED":
                logger.info("[master] VAD_STARTED    write_pos=%d", msg["write_pos"])

            elif cmd == "VAD_STOPPED":
                logger.info("[master] VAD_STOPPED    write_pos=%d", msg["write_pos"])

                # Stop the ring tail and wait for Deepgram to finalize.
                if capture is not None:
                    capture.stop_event.set()
                    if capture.thread is not None:
                        capture.thread.join(timeout=5)

                transcript = capture.get_transcript() if capture else ""
                capture = None

                pipe.send({"cmd": "SET_IDLE"})
                processing = True
                try:
                    cognitive_loop(transcript, agent)
                except Exception as exc:
                    logger.error("cognitive loop error: %s", exc)
                finally:
                    processing = False
                    try:
                        pipe.send({"cmd": "SET_WAKE_LISTEN"})
                    except (BrokenPipeError, OSError):
                        pass
                    logger.info("listening for wake word...")

            elif cmd == "SHUTDOWN_COMMENCED":
                logger.info("[master] child initiated shutdown")
                return

            elif cmd == "ERROR":
                logger.error("[master] ERROR from child: %s", msg.get("msg", "?"))

    finally:
        agent.close()
        if capture is not None:
            capture.stop_event.set()
            if capture.thread is not None:
                capture.thread.join(timeout=3)


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

    logger.info("[master] recorder child spawned (pid=%d)", child.pid)

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
