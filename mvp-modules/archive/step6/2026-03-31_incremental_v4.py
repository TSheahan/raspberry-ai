"""Incremental v4 — Add UtteranceCapturer to pipeline.

Pipeline: LocalAudioTransport.input() -> OpenWakeWordProcessor -> UtteranceCapturer
Tests whether UtteranceCapturer blocks CancelFrame propagation.
RTVI left at default (ON) — cleared by v2a.
"""

import asyncio
import os
import sys
import time
import numpy as np
from dotenv import load_dotenv

load_dotenv(override=True)

os.environ["ORT_LOG_LEVEL"] = "ERROR"

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.frames.frames import Frame, AudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor


class UtteranceCapturer(FrameProcessor):
    """Identical to v8's UtteranceCapturer."""

    def __init__(self):
        super().__init__()
        self.utterance_buffer = np.array([], dtype=np.int16)
        self.capturing = False

    def start_capture(self):
        self.capturing = True
        self.utterance_buffer = np.array([], dtype=np.int16)

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame) and self.capturing:
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self.utterance_buffer = np.append(self.utterance_buffer, audio_chunk)
        await self.push_frame(frame, direction)


class OpenWakeWordProcessor(FrameProcessor):
    """Identical to v8's OpenWakeWordProcessor, with capturer dependency."""

    def __init__(self, capturer):
        super().__init__()
        print("Loading openwakeword models...")
        from openwakeword.model import Model
        self.model = Model()
        self.buffer = np.array([], dtype=np.int16)
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self.capturer = capturer
        print("openwakeword ready")

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
                current_time = time.time()
                for wakeword, score in predictions.items():
                    if (wakeword == "hey_jarvis"
                            and score > 0.5
                            and (current_time - self.last_detection_time) > self.DEBOUNCE_SECONDS):
                        print(f"\nWAKE DETECTED -- '{wakeword}'  |  score: {score:.3f}")
                        self.last_detection_time = current_time
                        self.capturer.start_capture()
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

    pipeline = Pipeline([
        transport.input(),
        wake_processor,
        capturer,
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("Incremental v4 (Pipecat + OpenWakeWord + UtteranceCapturer): Listening...")
    print("   Say 'hey Jarvis' to test wake detection, then Ctrl+C to test shutdown.")
    print("   Press Ctrl+C to test shutdown.\n")

    await runner.run(task)

    # Report capture state for diagnostic purposes
    if capturer.capturing:
        print(f"Captured {len(capturer.utterance_buffer)} samples before shutdown.")
    else:
        print("No capture was active at shutdown.")

    print("Incremental v4 finished cleanly -- process should exit now.")


if __name__ == "__main__":
    asyncio.run(main())
