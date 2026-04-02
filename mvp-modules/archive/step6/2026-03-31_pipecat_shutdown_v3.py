import asyncio
import os
import numpy as np
import wave
import tempfile
import signal
import sys
import threading
import time
import openwakeword
from openwakeword.model import Model
from deepgram import DeepgramClient

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.frames.frames import Frame, AudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor

os.environ["ORT_LOG_LEVEL"] = "ERROR"

# ── Thread-safe shutdown coordination ─────────────────────────────────
shutdown_event = threading.Event()
main_loop = None  # will be set to the running asyncio loop

def handle_sigint(signum, frame):
    """Signal handler — safe from any thread."""
    print("\n🛑 SIGINT received — initiating clean Pipecat shutdown...")
    if main_loop:
        main_loop.call_soon_threadsafe(lambda: asyncio.create_task(shutdown_task()))

async def shutdown_task():
    """Runs in the main asyncio thread after SIGINT."""
    print("🧹 Running final shutdown sequence...")
    # Transcription + cleanup happen here (same as diagnostic)
    stt_processor._transcribe_sync()   # will be defined below
    await transport.cleanup()

    # Extra force-close for PyAudio/ALSA threads on Pi 4 (the part that fixed the hang)
    if hasattr(transport, '_audio_in_stream') and transport._audio_in_stream:
        try:
            transport._audio_in_stream.stop_stream()
            transport._audio_in_stream.close()
        except Exception:
            pass
    await asyncio.sleep(0.3)   # give ALSA threads time to die

    print("✅ Step 6 finished cleanly — process should exit now.")
    # No sys.exit — we let the pipeline task cancel naturally

class OpenWakeWordProcessor(FrameProcessor):
    def __init__(self, capturer):
        super().__init__()
        print("🔄 Loading openwakeword models...")
        self.model = Model()
        self.buffer = np.array([], dtype=np.int16)
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self.capturer = capturer
        print("✅ openwakeword ready")

    async def process_frame(self, frame: Frame, direction: str):
        try:
            await super().process_frame(frame, direction)
            if isinstance(frame, AudioRawFrame):
                audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
                self.buffer = np.append(self.buffer, audio_chunk)

                chunk_size = 1280
                while len(self.buffer) >= chunk_size:
                    chunk = self.buffer[:chunk_size]
                    self.buffer = self.buffer[chunk_size:]
                    predictions = self.model.predict(chunk.astype(np.float32))
                    current_time = time.time()
                    for wakeword, score in predictions.items():
                        if wakeword == "hey_jarvis" and score > 0.5 and (current_time - self.last_detection_time) > self.DEBOUNCE_SECONDS:
                            print(f"\n🔊 WAKE DETECTED — '{wakeword}'  |  score: {score:.3f}")
                            self.last_detection_time = current_time
                            self.capturer.start_capture()
            await self.push_frame(frame, direction)
        except asyncio.CancelledError:
            print("   OpenWakeWordProcessor cancelled")
            raise
        finally:
            await self.push_frame(frame, direction)

class UtteranceCapturer(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.utterance_buffer = np.array([], dtype=np.int16)
        self.capturing = False

    def start_capture(self):
        self.capturing = True
        self.utterance_buffer = np.array([], dtype=np.int16)

    async def process_frame(self, frame: Frame, direction: str):
        try:
            await super().process_frame(frame, direction)
            if isinstance(frame, AudioRawFrame) and self.capturing:
                audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
                self.utterance_buffer = np.append(self.utterance_buffer, audio_chunk)
            await self.push_frame(frame, direction)
        except asyncio.CancelledError:
            print("   UtteranceCapturer cancelled")
            raise
        finally:
            await self.push_frame(frame, direction)

class DeepgramSTTProcessor(FrameProcessor):
    def __init__(self, capturer):
        super().__init__()
        self.capturer = capturer
        self.dg_client = DeepgramClient()

    def _transcribe_sync(self):
        if len(self.capturer.utterance_buffer) == 0:
            print("ℹ️  No utterance buffer to transcribe")
            return
        buffer = self.capturer.utterance_buffer.copy()
        self.capturer.utterance_buffer = np.array([], dtype=np.int16)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp.name, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(buffer.tobytes())
            tmp_path = tmp.name

        print("🔄 Sending audio to Deepgram Nova-3...")
        try:
            with open(tmp_path, "rb") as audio_file:
                response = self.dg_client.listen.v1.media.transcribe_file(
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

# ── Global references so shutdown_task can access them ───────────────
transport = None
stt_processor = None

async def main():
    global transport, stt_processor, main_loop
    main_loop = asyncio.get_running_loop()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,
            audio_out_enabled=False,
        )
    )

    capturer = UtteranceCapturer()
    wake_processor = OpenWakeWordProcessor(capturer)
    stt_processor = DeepgramSTTProcessor(capturer)

    pipeline = Pipeline([
        transport.input(),
        wake_processor,
        capturer,
        stt_processor,
    ])

    runner = PipelineRunner(handle_sigint=False)
    task = PipelineTask(pipeline)

    print("🎤 Step 6 — Pipecat with proven shutdown (v3): Listening for 'hey jarvis' …")
    print("   After wake word, speak a short sentence then pause.")
    print("   Press Ctrl+C when finished speaking.\n")

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        print("\n🛑 Pipeline task cancelled cleanly by framework")
    except Exception as e:
        print(f"\n⚠️  Unexpected error: {e}")
    finally:
        print("\n🧹 Final cleanup after pipeline exit...")
        # Extra safety net — in case shutdown_task didn't run
        if stt_processor:
            stt_processor._transcribe_sync()
        if transport:
            await transport.cleanup()
        print("✅ Step 6 finished.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    asyncio.run(main())
