"""
master.py — Master process for the forked assistant (EU-4: batch mode).

Main entry point. Creates SharedMemory and Pipe, spawns the recorder child
on core 0, then runs a synchronous event loop that:

  1. Waits for READY from the recorder child
  2. Sends SET_WAKE_LISTEN
  3. On WAKE_DETECTED: sends SET_CAPTURE
  4. On VAD_STOPPED: reads the ring buffer span, transcribes via Deepgram,
     sends to Claude, prints the response
  5. On response complete: sends SET_WAKE_LISTEN (already sent before
     the cognitive loop — recorder listens while master thinks)
  6. Handles Ctrl+C → SHUTDOWN sequence

Dependencies not in requirements.txt (already installed on Pi venv):
  deepgram-sdk, python-dotenv

Usage (on Pi with ReSpeaker):
    cd ~/raspberry-ai/mvp-modules/forked_assistant
    source ~/pipecat-agent/venv/bin/activate
    python src/master.py
"""

import datetime
import logging
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

from multiprocessing import Pipe, Process
from multiprocessing.shared_memory import SharedMemory

from dotenv import load_dotenv

load_dotenv(override=True)

from deepgram import DeepgramClient

from log_config import configure_logging, TRACE

logger = logging.getLogger("master")

# Set SAVE_CAPTURE_WAV=<path> to write each ring-buffer span to disk before STT.
# The value must be a path to an existing directory; ~ is expanded.
# When unset or empty, saving is disabled. No separate flag needed.
# Example: SAVE_CAPTURE_WAV=~/raspberry-ai/scratch/executions python master.py
_WAV_SCRATCH_DIR = os.path.expanduser(os.environ.get("SAVE_CAPTURE_WAV", ""))

from recorder_child import recorder_child_entry
from ring_buffer import (
    CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH,
    SHM_NAME, SHM_SIZE,
    RingBufferReader,
)


# ---------------------------------------------------------------------------
# Agentic layer — claude CLI on Pi
# ---------------------------------------------------------------------------

def run_claude(transcript: str) -> str:
    result = subprocess.run(
        ["claude", "-p", transcript, "--model", "claude-haiku-4-5-20251001"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        logger.error("[claude] subprocess error: %s", err)
        return f"[claude error: {err}]"
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# STT — Deepgram file-based (batch) transcription
# ---------------------------------------------------------------------------

def transcribe(audio_bytes: bytes, dg_client: DeepgramClient) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        with wave.open(tmp.name, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_bytes)
        tmp_path = tmp.name

    logger.info("sending audio to Deepgram Nova-3...")
    try:
        with open(tmp_path, "rb") as f:
            response = dg_client.listen.v1.media.transcribe_file(
                request=f.read(),
                model="nova-3",
                smart_format=True,
                language="en",
            )
        try:
            transcript = response.results.channels[0].alternatives[0].transcript.strip()
        except (AttributeError, IndexError, TypeError) as e:
            logger.warning("[dg] response structure unexpected: %s", e)
            logger.debug("[dg] raw response: %s", response)
            return ""
        if not transcript:
            logger.warning("[dg] response ok but transcript is empty (confidence=%.3f)",
                           response.results.channels[0].alternatives[0].confidence)
        return transcript
    except Exception as e:
        logger.error("Deepgram error: %s", e)
        return ""
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# WAV debug save — preserves each captured span for offline inspection
# ---------------------------------------------------------------------------

def _save_wav_debug(audio_bytes: bytes) -> None:
    """Write audio_bytes to a timestamped WAV file when SAVE_CAPTURE_WAV is set.

    SAVE_CAPTURE_WAV must be a path to an existing directory. Prints a warning
    and skips silently if the directory does not exist.
    """
    if not _WAV_SCRATCH_DIR:
        return
    if not os.path.isdir(_WAV_SCRATCH_DIR):
        logger.warning("[wav] SAVE_CAPTURE_WAV=%r is not an existing directory — skipping",
                       _WAV_SCRATCH_DIR)
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    path = os.path.join(_WAV_SCRATCH_DIR, f"{ts}_capture.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_bytes)
    logger.debug("[wav] saved → %s", path)


# ---------------------------------------------------------------------------
# Cognitive loop — STT + Claude with timing
# ---------------------------------------------------------------------------

def cognitive_loop(audio_bytes: bytes, dg_client: DeepgramClient) -> None:
    duration = len(audio_bytes) / (SAMPLE_RATE * SAMPLE_WIDTH)
    logger.info("captured %.1fs of audio", duration)
    _save_wav_debug(audio_bytes)
    loop_start = time.time()

    transcript = transcribe(audio_bytes, dg_client)
    stt_elapsed = time.time() - loop_start

    if not transcript:
        logger.warning("no transcript returned")
        return

    logger.info("TRANSCRIPT: %s", transcript)
    logger.info("STT latency: %.2fs", stt_elapsed)

    claude_start = time.time()
    response = run_claude(transcript)
    claude_elapsed = time.time() - claude_start

    logger.info("CLAUDE RESPONSE:\n%s", response)
    logger.info("Claude latency: %.2fs", claude_elapsed)
    logger.info("total loop latency: %.2fs", time.time() - loop_start)


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
    processing = False
    wake_pos = 0
    vad_start_pos = 0

    msg = pipe.recv()
    if msg["cmd"] != "READY":
        raise RuntimeError(f"Expected READY, got {msg}")
    logger.info("[master] recorder child READY")

    pipe.send({"cmd": "SET_WAKE_LISTEN"})
    logger.info("listening for wake word...")

    while True:
        msg = pipe.recv()
        cmd = msg["cmd"]

        if cmd == "STATE_CHANGED":
            logger.debug("[master] state -> %s", msg['state'])

        elif cmd == "WAKE_DETECTED":
            if processing:
                logger.warning("[master] still processing previous utterance, ignoring wake")
                continue
            wake_pos = msg["write_pos"]
            logger.info("[master] WAKE_DETECTED  score=%.3f  keyword=%s",
                        msg['score'], msg['keyword'])
            pipe.send({"cmd": "SET_CAPTURE"})

        elif cmd == "VAD_STARTED":
            vad_start_pos = msg["write_pos"]
            logger.info("[master] VAD_STARTED    write_pos=%d", vad_start_pos)

        elif cmd == "VAD_STOPPED":
            end_pos = msg["write_pos"]
            # Always read from wake_pos — captures the full utterance including
            # pre-speech audio that precedes the Silero onset delay (~0.2s
            # start_secs plus processing). vad_start_pos is logged as advisory
            # so the onset gap is visible, but it is not used as the span start.
            start = wake_pos
            span = end_pos - start
            dur_s = span / (SAMPLE_RATE * SAMPLE_WIDTH)
            live_wp = ring_reader.write_pos
            stale = ring_reader.is_stale(start)
            vad_gap = vad_start_pos - wake_pos if vad_start_pos else 0
            vad_gap_s = vad_gap / (SAMPLE_RATE * SAMPLE_WIDTH)
            logger.info("[master] VAD_STOPPED    write_pos=%d", end_pos)
            logger.debug("[ring] span: start=%d(wake)  end=%d  bytes=%d  dur=%.2fs",
                         start, end_pos, span, dur_s)
            logger.debug("[ring] vad_start=%d  vad_gap=%db (%.2fs pre-speech dropped previously)",
                         vad_start_pos, vad_gap, vad_gap_s)
            logger.debug("[ring] live write_pos=%d  stale=%s", live_wp, stale)

            audio_bytes = ring_reader.read(start, end_pos)

            if not audio_bytes:
                logger.warning("[ring] read returned empty  (span=%d  stale=%s)", span, stale)
            else:
                n_samples = len(audio_bytes) // SAMPLE_WIDTH
                samples = struct.unpack_from(f'<{n_samples}h', audio_bytes)
                zero_samples = samples.count(0)
                rms = (sum(s * s for s in samples) / n_samples) ** 0.5
                head = samples[:8]
                tail = samples[-4:]
                logger.debug("[ring] read ok: %d bytes  %d samples  zeros=%d  rms=%.1f",
                             len(audio_bytes), n_samples, zero_samples, rms)
                logger.log(TRACE, "[ring] head=%s  tail=%s", head, tail)

            pipe.send({"cmd": "SET_WAKE_LISTEN"})
            processing = True
            try:
                cognitive_loop(audio_bytes, dg_client)
            except Exception as e:
                logger.error("cognitive loop error: %s", e)
            finally:
                processing = False
                logger.info("listening for wake word...")

        elif cmd == "SHUTDOWN_COMMENCED":
            logger.info("[master] child initiated shutdown")
            return

        elif cmd == "ERROR":
            logger.error("[master] ERROR from child: %s", msg.get('msg', '?'))


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
