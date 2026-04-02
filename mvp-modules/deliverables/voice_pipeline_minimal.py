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
    """Exact same logic as your proven standalone script (0.4.0 compatible)."""
    def __init__(self):
        super().__init__()
        print("🔄 Loading openwakeword models...")
        self.model = Model()          # ← NO arguments in 0.4.0 (loads all models)
        self.buffer = np.array([], dtype=np.int16)
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        print("✅ openwakeword ready (listening for 'hey_jarvis')")

    async def process_frame(self, frame: Frame, direction: str):
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
                        if hasattr(self, "task") and self.task:
                            self.task.cancel()
                            return

        await self.push_frame(frame, direction)

async def main():
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,      # ReSpeaker
            audio_out_enabled=False,
            vad_enabled=False,
        )
    )

    wake_processor = OpenWakeWordProcessor()
    wake_processor.task = None

    pipeline = Pipeline([transport.input(), wake_processor])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)
    wake_processor.task = task

    print("🎤 Pipecat minimal pipeline: Listening for 'hey jarvis' on ReSpeaker...")
    print("   (Speak normally ~1 m away — should behave exactly like your standalone script)")

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        print("\nPipeline stopped after wake-word detection.")
    except KeyboardInterrupt:
        print("\nManually stopped.")
    finally:
        await transport.cleanup()
        print("✅ Step 4 minimal wake-word pipeline finished.")

if __name__ == "__main__":
    asyncio.run(main())
