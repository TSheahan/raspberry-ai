import asyncio
import os
import numpy as np
import openwakeword
from openwakeword.model import Model

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
        print("✅ openwakeword ready (listening for 'hey_jarvis')")

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
                        self.capturer.start_capture()   # ← signal capturer
                        # Do NOT cancel — let utterance be captured

        await self.push_frame(frame, direction)

class UtteranceCapturer(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.utterance_buffer = np.array([], dtype=np.int16)
        self.capturing = False
        self.capture_event = asyncio.Event()

    def start_capture(self):
        self.capturing = True
        self.capture_event.set()

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame) and self.capturing:
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self.utterance_buffer = np.append(self.utterance_buffer, audio_chunk)

        await self.push_frame(frame, direction)

async def main():
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,      # ReSpeaker
            audio_out_enabled=False,
        )
    )

    capturer = UtteranceCapturer()
    wake_processor = OpenWakeWordProcessor(capturer)

    pipeline = Pipeline([
        transport.input(),
        wake_processor,
        capturer,
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("🎤 Step 5: Listening for 'hey jarvis' …")
    print("   After wake word, speak a short sentence then pause.")
    print("   Press Ctrl+C when you are finished speaking.\n")

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        print("\nManually stopped.")
    finally:
        if len(capturer.utterance_buffer) > 0:
            duration_sec = len(capturer.utterance_buffer) / 16000.0
            print(f"\n✅ VAD-style capture complete: {duration_sec:.2f} seconds of audio")
        else:
            print("\nNo utterance captured.")
        await transport.cleanup()
        print("✅ Step 5 finished.")

if __name__ == "__main__":
    asyncio.run(main())
