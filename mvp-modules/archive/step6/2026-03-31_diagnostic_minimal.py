import asyncio
import signal
import sys
import numpy as np
import wave
import tempfile
import os
import pyaudio
from openwakeword.model import Model
from deepgram import DeepgramClient

os.environ["ORT_LOG_LEVEL"] = "ERROR"

# ── Configuration ─────────────────────────────────────────────────────
INPUT_DEVICE_INDEX = 1          # ReSpeaker
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280               # openwakeword expects 1280 samples
DEBOUNCE_SECONDS = 1.8

# ── Globals ───────────────────────────────────────────────────────────
shutdown_event = asyncio.Event()
utterance_buffer = np.array([], dtype=np.int16)
capturing = False
last_detection_time = 0.0
wake_model = None
dg_client = None
audio_stream = None

def audio_callback(in_data, frame_count, time_info, status):
    """PyAudio callback — runs in a separate thread."""
    global utterance_buffer, capturing, last_detection_time

    audio_chunk = np.frombuffer(in_data, dtype=np.int16)

    # Wake-word detection
    predictions = wake_model.predict(audio_chunk.astype(np.float32))
    current_time = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0

    for wakeword, score in predictions.items():
        if (wakeword == "hey_jarvis" and 
            score > 0.5 and 
            (current_time - last_detection_time) > DEBOUNCE_SECONDS):
            print(f"\n🔊 WAKE DETECTED — '{wakeword}'  |  score: {score:.3f}")
            last_detection_time = current_time
            global capturing
            capturing = True
            utterance_buffer = np.array([], dtype=np.int16)   # reset for new utterance

    # Capture if after wake word
    if capturing:
        utterance_buffer = np.append(utterance_buffer, audio_chunk)

    return (in_data, pyaudio.paContinue)

def _transcribe_sync():
    """Same transcription logic as before — runs on shutdown."""
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
    """Clean shutdown sequence."""
    print("\n🛑 SIGINT received — shutting down audio stream...")
    
    global audio_stream
    if audio_stream:
        audio_stream.stop_stream()
        audio_stream.close()
    pyaudio.PyAudio().terminate()   # force PyAudio cleanup

    print("🧹 Running final transcription...")
    _transcribe_sync()

    print("✅ Diagnostic finished cleanly — process should exit now.")
    sys.exit(0)

def handle_sigint(signum, frame):
    """Signal handler — schedules clean shutdown."""
    asyncio.create_task(shutdown())

async def main():
    global wake_model, dg_client, audio_stream

    print("🔄 Loading openwakeword model...")
    wake_model = Model()
    print("✅ openwakeword ready")

    dg_client = DeepgramClient()
    print("✅ Deepgram client ready")

    # PyAudio setup
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

    print("🎤 Minimal diagnostic (no Pipecat): Listening for 'hey jarvis' …")
    print("   After wake word, speak a short sentence then pause.")
    print("   Press Ctrl+C when finished speaking.\n")

    # Keep the event loop alive until shutdown
    try:
        await shutdown_event.wait()   # never actually set — shutdown via signal
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    asyncio.run(main())
