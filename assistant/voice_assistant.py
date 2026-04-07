"""
voice_assistant.py — Master process for the forked assistant (EU-5/EU-6/EU-7: streaming + TTS).

Main entry point. Creates SharedMemory and Pipe, spawns the recorder child
on core 0, then runs a synchronous event loop that:

  1. Waits for READY from the recorder child
  2. Sends SET_WAKE_LISTEN
  3. On WAKE_DETECTED: pre-spawns agent subprocess (EU-6); sends SET_CAPTURE; on
     STATE_CHANGED(capture) starts Deepgram live WebSocket + ring-tail thread (EU-5)
  4. On VAD_STOPPED: stops ring-tail, finalizes Deepgram stream, assembles transcript,
     calls cognitive_loop: primes TTS (warm) in parallel with agent.run(), then
     tts.play() for speech output (EU-7)
  5. On response complete: sends SET_WAKE_LISTEN
  6. Handles Ctrl+C → SHUTDOWN sequence

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
import threading
import time

from multiprocessing import Pipe, Process
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

from deepgram import DeepgramClient
from deepgram.core.events import EventType

from logging_setup import configure_logging
from agent_session import AgentSession, CursorAgentSession, AgentError
from master_state import MasterState
from recorder_process import recorder_child_entry
from tts_backends import TTSBackend, CartesiaTTS, ElevenLabsTTS, DeepgramTTS
from audio_shm_ring import (
    CHANNELS, SAMPLE_RATE,
    SHM_NAME, SHM_SIZE,
    AudioShmRingReader,
)

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

# Deepgram keepalive: send every N seconds when ring write_pos has not advanced.
# Deepgram closes the WebSocket with NET-0001 after 10 s of silence.
_DG_KEEPALIVE_INTERVAL = 3.5


# ---------------------------------------------------------------------------
# Streaming capture session — ring tail + Deepgram live WebSocket
# ---------------------------------------------------------------------------

class _SttCaptureSession:
    """State for one WAKE_DETECTED → VAD_STOPPED STT (Deepgram) capture window."""

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
    capture: _SttCaptureSession,
    ring_reader: AudioShmRingReader,
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
        # Note: post-CloseStream Results with from_finalize=false have been
        # observed in practice (EU-5 runs 2026-04-04) and have been consistently
        # empty. Handled correctly by the is_final guard above; no action needed.
        try:
            text = message.channel.alternatives[0].transcript.strip()
            if text:
                capture.add_transcript(text)
                logger.debug("[dg-live] is_final: {!r}", text)
        except Exception as exc:
            logger.debug("[dg-live] on_message parse error: {}", exc)

    def on_error(error) -> None:
        logger.error("[dg-live] error: {}", error)

    try:
        with dg_client.listen.v1.connect(
            model="nova-3",
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            language="en-US",
            smart_format="true",
            interim_results="true",
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
            # 200ms pause lets Deepgram flush trailing results before CloseStream.
            # This blocks master for ~460ms total (sleep + WS close + listen join)
            # before SET_IDLE and cognitive_loop can start. A potential optimisation
            # is to feed the transcript to the agent immediately at VAD_STOPPED and
            # skip waiting for finalize results. Timing analysis (EU-5 runs 2026-04-04)
            # shows this is low-risk — the definitive is_final arrives well before
            # VAD_STOPPED in practice — but agents do not accept supplemental input
            # while responding, so any trailing Deepgram segment would be silently
            # lost. Adopt only once the chance of trailing-text loss is satisfactorily
            # characterised across longer utterances.
            time.sleep(0.2)
            conn.send_close_stream()
            listen_thread.join(timeout=2)

    except Exception as exc:
        logger.error("[dg-live] capture session error: {}", exc)


# ---------------------------------------------------------------------------
# Cognitive loop — agent response + TTS output (EU-6/EU-7)
# ---------------------------------------------------------------------------

def _arm_stt_session(
    state: MasterState,
    ring_reader: AudioShmRingReader,
    dg_client: DeepgramClient,
) -> None:
    """Start Deepgram + ring tail once belief is capture (STATE_CHANGED) and SET_CAPTURE was sent."""
    cap = _SttCaptureSession()
    wake_pos = state.arm_stt(cap)
    if wake_pos < 0:
        return
    cap.thread = threading.Thread(
        target=_run_capture,
        args=(cap, ring_reader, wake_pos, dg_client),
        daemon=True,
    )
    cap.thread.start()


def cognitive_loop(transcript: str, agent: AgentSession, tts: TTSBackend) -> None:
    """Feed transcript to agent; synthesise and play each yielded sentence chunk."""
    if not transcript:
        logger.warning("no transcript — skipping cognitive loop")
        return
    logger.info("TRANSCRIPT: {}", transcript)
    loop_start = time.monotonic()
    try:
        # Warm overlaps agent time-to-first-token (not at wake — avoids priming TTS
        # during long dictation-only idle after wake).
        threading.Thread(target=tts.warm, daemon=True).start()
        tts.play(agent.run(transcript))
    except AgentError as exc:
        logger.error("[agent] error: {}", exc)
    logger.info("cognitive loop latency: {:.2f}s", time.monotonic() - loop_start)


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
    state = MasterState()

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
                res = state.on_state_changed(msg["state"])
                if res.accepted and state.stt_arm_ready:
                    _arm_stt_session(state, ring_reader, dg_client)
                    print("!! SPEAK !!", flush=True)
                elif res.accepted and state.capture_phase_without_pending_stt:
                    logger.warning(
                        "[master] STATE_CHANGED(capture) but STT not pending — "
                        "possible protocol skew",
                    )

            elif cmd == "WAKE_DETECTED":
                if not state.on_wake_detected(
                    msg["write_pos"], msg["score"], msg["keyword"]
                ):
                    logger.warning(
                        "[master] WAKE_DETECTED ignored (processing={} phase={})",
                        state.processing,
                        state.phase,
                    )
                    continue
                logger.info("[master] WAKE_DETECTED  score={:.3f}  keyword={}",
                            msg["score"], msg["keyword"])

                # Pre-spawn agent (hides startup latency behind the STT window).
                # Deepgram + ring tail start on STATE_CHANGED(capture) (master_state_spec §2d).
                agent.prepare()
                state.note_agent_prepare()
                pipe.send({"cmd": "SET_CAPTURE"})
                state.mark_stt_pending_after_set_capture()

            elif cmd == "VAD_STARTED":
                if state.on_vad_started(msg["write_pos"]):
                    logger.info("[master] VAD_STARTED    write_pos={}", msg["write_pos"])

            elif cmd == "VAD_STOPPED":
                if not state.on_vad_stopped(msg["write_pos"]):
                    logger.debug(
                        "[master] VAD_STOPPED ignored (phase={} vad_speaking={})",
                        state.phase,
                        state.vad_speaking,
                    )
                    continue
                logger.info("[master] VAD_STOPPED    write_pos={}", msg["write_pos"])

                transcript = state.finalize_capture()

                pipe.send({"cmd": "SET_IDLE"})
                state.begin_processing()
                try:
                    cognitive_loop(transcript, agent, tts)
                except Exception as exc:
                    logger.error("cognitive loop error: {}", exc)
                finally:
                    state.end_processing()
                    try:
                        pipe.send({"cmd": "SET_WAKE_LISTEN"})
                    except (BrokenPipeError, OSError):
                        pass
                    logger.info("listening for wake word...")

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
        agent.close()
        tts.close()
        state.teardown_capture()


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
