import asyncio
import os
import numpy as np
import wave
import tempfile
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
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self.buffer = np.append(self.buffer, audio_chunk)

            chunk_size = 1280
            while len(self.buffer) >= chunk_size:
                chunk = self.buffer[:chunk_size]
                self.buffer = self.buffer[chunk_size:]
                predictions = self.model.predict(chunk.astype(np.float32))
                current_time = asyncio.get_event_loop().time()
                for wakeword, score in predictions.items():
                    if wakeword == "hey_jarvis" and score > 0.5 and (current_time - self.last_detection_time) > self.DEBOUNCE_SECONDS:
                        print(f"\n🔊 WAKE DETECTED — '{wakeword}'  |  score: {score:.3f}")
                        self.last_detection_time = current_time
                        self.capturer.start_capture()
        await self.push_frame(frame, direction)

class UtteranceCapturer(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.utterance_buffer = np.array([], dtype=np.int16)
        self.capturing = False

    def start_capture(self):
        self.capturing = True

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame) and self.capturing:
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self.utterance_buffer = np.append(self.utterance_buffer, audio_chunk)
        await self.push_frame(frame, direction)

class DeepgramSTTProcessor(FrameProcessor):
    def __init__(self, capturer):
        super().__init__()
        self.capturer = capturer
        self.dg_client = DeepgramClient()

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)

        # When user stops speaking (Ctrl+C) and we have audio, transcribe
        if isinstance(frame, AudioRawFrame) and not self.capturer.capturing and len(self.capturer.utterance_buffer) > 0:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                with wave.open(tmp.name, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(self.capturer.utterance_buffer.tobytes())
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
                self.capturer.utterance_buffer = np.array([], dtype=np.int16)  # reset for next turn

        await self.push_frame(frame, direction)

async def main():
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

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("🎤 Step 6: Listening for 'hey jarvis' …")
    print("   After wake word, speak a short sentence then pause.")
    print("   Press Ctrl+C when finished speaking.\n")

    try:
        await runner.run(task)
    except KeyboardInterrupt:
        print("\nManually stopped.")
    finally:
        await transport.cleanup()
        print("✅ Step 6 finished.")

if __name__ == "__main__":
    asyncio.run(main())
