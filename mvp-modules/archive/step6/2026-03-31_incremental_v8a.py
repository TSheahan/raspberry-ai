"""Incremental v8a — Method naming + code comments (cluster E).

Same as v7 but with:
  E1. Rename transcribe_sync -> _transcribe_sync (match v8's leading underscore)
  E2. Add v8's inline comment above runner = PipelineRunner()
  E3. Remove explicit process_frame from DeepgramSTTProcessor (v8 relies on parent default)
"""

import asyncio
import os
import sys
import time
import tempfile
import wave
import numpy as np
from dotenv import load_dotenv

load_dotenv(override=True)

from openwakeword.model import Model
from deepgram import DeepgramClient

if not os.environ.get("DEEPGRAM_API_KEY"):
    sys.exit("DEEPGRAM_API_KEY not set. Check ~/.env")

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.frames.frames import Frame, AudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor

os.environ["ORT_LOG_LEVEL"] = "ERROR"


class OpenWakeWordProcessor(FrameProcessor):
    """Same as v7."""

    def __init__(self, capturer):
        super().__init__()
        print("Loading openwakeword models...")
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


class UtteranceCapturer(FrameProcessor):
    """Same as v7."""

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


class DeepgramSTTProcessor(FrameProcessor):
    """Same as v7 but: no explicit process_frame (match v8), rename to _transcribe_sync."""

    def __init__(self, capturer):
        super().__init__()
        self.capturer = capturer
        self.dg_client = DeepgramClient()

    # --- CHANGE E3: No explicit process_frame — v8 relies on FrameProcessor default ---

    # --- CHANGE E1: Renamed from transcribe_sync to _transcribe_sync (match v8) ---
    def _transcribe_sync(self):
        if len(self.capturer.utterance_buffer) == 0:
            print("No utterance buffer to transcribe.")
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

        print("Sending audio to Deepgram Nova-3...")
        try:
            with open(tmp_path, "rb") as audio_file:
                response = self.dg_client.listen.v1.media.transcribe_file(
                    request=audio_file.read(),
                    model="nova-3",
                    smart_format=True,
                    language="en"
                )
            transcript = response.results.channels[0].alternatives[0].transcript.strip()
            print(f"\nTRANSCRIPT: {transcript}")
        except Exception as e:
            print(f"Deepgram error: {e}")
        finally:
            os.unlink(tmp_path)


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

    # --- CHANGE E2: Add v8's inline comment above runner ---
    # Let PipelineRunner handle SIGINT — it queues a CancelFrame that
    # propagates through the pipeline and triggers LocalAudioTransport
    # cleanup (stops PyAudio streams) automatically.
    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("Incremental v8a (v7 + method naming/comments): Listening...")
    print("   Say 'hey Jarvis', speak a short sentence, then Ctrl+C to test shutdown.")
    print("   Press Ctrl+C to test shutdown.\n")

    await runner.run(task)

    # Post-pipeline: transcribe any captured audio
    print("\nRunning final transcription...")
    stt_processor._transcribe_sync()

    print("Incremental v8a finished cleanly -- process should exit now.")


if __name__ == "__main__":
    asyncio.run(main())
