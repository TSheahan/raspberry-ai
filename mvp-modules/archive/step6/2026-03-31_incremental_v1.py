import asyncio
import signal
import sys
import numpy as np
import wave
import tempfile
import os
import pyaudio
import threading
import time
from openwakeword.model import Model
from deepgram import DeepgramClient
from dotenv import load_dotenv

load_dotenv(override=True)

API_KEY = os.environ.get("DEEPGRAM_API_KEY")
if not API_KEY:
    sys.exit("❌ DEEPGRAM_API_KEY not set. Check ~/.env")

os.environ["ORT_LOG_LEVEL"] = "ERROR"

# ── Configuration ─────────────────────────────────────────────────────
INPUT_DEVICE_INDEX = 1
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280
DEBOUNCE_SECONDS = 1.8

# ── Globals (thread-safe) ─────────────────────────────────────────────
shutdown_event = asyncio.Event()
utterance_buffer = np.array([], dtype=np.int16)
capturing = False
last_detection_time = 0.0
wake_model = None
dg_client = None
audio_stream = None
loop = None

def audio_callback(in_data, frame_count, time_info, status):
    """PyAudio callback — runs in a separate thread. No asyncio calls."""
    global utterance_buffer, capturing, last_detection_time

    audio_chunk = np.frombuffer(in_data, dtype=np.int16)

    predictions = wake_model.predict(audio_chunk.astype(np.float32))
    current_time = time.time()

    for wakeword, score in predictions.items():
        if (wakeword == "hey_jarvis" and 
            score > 0.5 and 
            (current_time - last_detection_time) > DEBOUNCE_SECONDS):
            print(f"\n🔊 WAKE DETECTED — '{wakeword}'  |  score: {score:.3f}")
            last_detection_time = current_time
            capturing = True
            utterance_buffer = np.array([], dtype=np.int16)

    if capturing:
        utterance_buffer = np.append(utterance_buffer, audio_chunk)

    return (in_data, pyaudio.paContinue)

def _transcribe_sync():
    global utterance_buffer
    if len(utterance_buffer) == 0:
        print("ℹ️  No utterance buffer to transcribe")
        return

    buffer = utterance_buffer.copy()
    utterance_buffer = np.array([], dtype=np.int16)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        with wave.open(tmp.name, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(buffer.tobytes())
        tmp_path = tmp.name

    print("🔄 Sending audio to Deepgram Nova-3...")
    try:
        with open(tmp_path, "rb") as audio_file:
            response = dg_client.listen.v1.media.transcribe_file(
                request=audio_file.read(),
                model="nova-3",
                smart_format=True,
                language="en"
            )
        transcript = response.results.channels[0].alternatives[0].transcript.strip()
        print(f"\n📝 TRANSCRIPT: {transcript}")
    except Exception as e:
        print(f"❌ Deepgram error: {e}")
    finally:
        os.unlink(tmp_path)

async def shutdown():
    """Clean shutdown — runs in main asyncio thread."""
    print("\n🛑 Shutting down audio stream...")
    global audio_stream
    if audio_stream:
        audio_stream.stop_stream()
        audio_stream.close()
    pyaudio.PyAudio().terminate()

    print("🧹 Running final transcription...")
    _transcribe_sync()

    print("✅ Incremental v1 finished cleanly — process should exit now.")

    # THIS WAS THE MISSING LINE IN v3
    shutdown_event.set()

def handle_sigint(signum, frame):
    """Signal handler — safe from any thread."""
    if loop:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(shutdown()))

async def main():
    global wake_model, dg_client, audio_stream, loop
    loop = asyncio.get_running_loop()

    print("🔄 Loading openwakeword model...")
    wake_model = Model()
    print("✅ openwakeword ready")

    dg_client = DeepgramClient(api_key=API_KEY)
    print("✅ Deepgram client ready")

    p = pyaudio.PyAudio()
    audio_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=INPUT_DEVICE_INDEX,
        frames_per_buffer=CHUNK_SIZE,
        stream_callback=audio_callback
    )
    audio_stream.start_stream()

    print("🎤 Incremental v1 (baseline — diagnostic v4 copy): Listening for 'hey jarvis' …")
    print("   After wake word, speak a short sentence then pause.")
    print("   Press Ctrl+C when finished speaking.\n")

    await shutdown_event.wait()   # now properly unblocked by shutdown()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    asyncio.run(main())
