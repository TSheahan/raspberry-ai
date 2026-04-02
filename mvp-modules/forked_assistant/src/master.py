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

import os
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
        return f"[claude error: {result.stderr.strip()}]"
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

    print("  Sending audio to Deepgram Nova-3...")
    try:
        with open(tmp_path, "rb") as f:
            response = dg_client.listen.v1.media.transcribe_file(
                request=f.read(),
                model="nova-3",
                smart_format=True,
                language="en",
            )
        return response.results.channels[0].alternatives[0].transcript.strip()
    except Exception as e:
        print(f"  Deepgram error: {e}")
        return ""
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Cognitive loop — STT + Claude with timing
# ---------------------------------------------------------------------------

def cognitive_loop(audio_bytes: bytes, dg_client: DeepgramClient) -> None:
    duration = len(audio_bytes) / (SAMPLE_RATE * SAMPLE_WIDTH)
    print(f"  Captured {duration:.1f}s of audio")
    loop_start = time.time()

    transcript = transcribe(audio_bytes, dg_client)
    stt_elapsed = time.time() - loop_start

    if not transcript:
        print("  No transcript returned.")
        return

    print(f"  TRANSCRIPT: {transcript}")
    print(f"  STT latency: {stt_elapsed:.2f}s")

    claude_start = time.time()
    response = run_claude(transcript)
    claude_elapsed = time.time() - claude_start

    print(f"\n  CLAUDE RESPONSE:\n  {response}\n")
    print(f"  Claude latency: {claude_elapsed:.2f}s")
    print(f"  Total loop latency: {time.time() - loop_start:.2f}s")


# ---------------------------------------------------------------------------
# Child shutdown sequence (spec §3)
# ---------------------------------------------------------------------------

def shutdown_child(pipe, child: Process) -> None:
    try:
        pipe.send({"cmd": "SHUTDOWN"})
    except Exception:
        pass
    child.join(timeout=3)
    if child.is_alive():
        print("[master] child did not exit — terminating")
        child.terminate()
        child.join(timeout=2)
    if child.is_alive():
        print("[master] child still alive — killing")
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
    print("[master] recorder child READY")

    pipe.send({"cmd": "SET_WAKE_LISTEN"})
    print("Listening for wake word...\n")

    while True:
        msg = pipe.recv()
        cmd = msg["cmd"]

        if cmd == "STATE_CHANGED":
            print(f"[master] state -> {msg['state']}")

        elif cmd == "WAKE_DETECTED":
            if processing:
                print("[master] (still processing previous utterance, ignoring wake)")
                continue
            wake_pos = msg["write_pos"]
            print(f"[master] WAKE_DETECTED  score={msg['score']:.3f}  "
                  f"keyword={msg['keyword']}")
            pipe.send({"cmd": "SET_CAPTURE"})

        elif cmd == "VAD_STARTED":
            vad_start_pos = msg["write_pos"]
            print(f"[master] VAD_STARTED    write_pos={vad_start_pos}")

        elif cmd == "VAD_STOPPED":
            end_pos = msg["write_pos"]
            start = vad_start_pos or wake_pos
            audio_bytes = ring_reader.read(start, end_pos)
            print(f"[master] VAD_STOPPED    write_pos={end_pos}")

            pipe.send({"cmd": "SET_WAKE_LISTEN"})
            processing = True
            try:
                cognitive_loop(audio_bytes, dg_client)
            except Exception as e:
                print(f"  Cognitive loop error: {e}")
            finally:
                processing = False
                print("\nListening for wake word...\n")

        elif cmd == "ERROR":
            print(f"[master] ERROR from child: {msg.get('msg', '?')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
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

    print(f"[master] recorder child spawned (pid={child.pid})")

    try:
        master_loop(parent_conn, shm, child)
    except KeyboardInterrupt:
        print("\n[master] Ctrl+C — shutting down")
        shutdown_child(parent_conn, child)
    except EOFError:
        print("[master] pipe broken — recorder child likely crashed")
    finally:
        parent_conn.close()
        try:
            shm.unlink()
        except FileNotFoundError:
            pass
        shm.close()

    print("[master] done")


if __name__ == "__main__":
    main()
