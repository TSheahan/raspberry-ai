"""Incremental v6 — Import timing alignment (clusters A+B+C).

Same as v5 but with three import-timing changes to match v8:
  A. openwakeword.model.Model imported at top level (not lazy inside __init__)
  B. DeepgramClient imported at top level, before ORT_LOG_LEVEL
  C. ORT_LOG_LEVEL set after all imports (not before)

If this breaks clean exit, one of these libraries installs a signal handler
or starts a thread at import time that interferes with Pipecat's SIGINT handling.
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

# --- CHANGE A: openwakeword imported at top level (was lazy in v5) ---
from openwakeword.model import Model
# --- CHANGE B: DeepgramClient imported at top level (was after ORT_LOG_LEVEL in v5) ---
from deepgram import DeepgramClient

if not os.environ.get("DEEPGRAM_API_KEY"):
    sys.exit("DEEPGRAM_API_KEY not set. Check ~/.env")

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.frames.frames import Frame, AudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor

# --- CHANGE C: ORT_LOG_LEVEL set after all imports (was before pipecat imports in v5) ---
os.environ["ORT_LOG_LEVEL"] = "ERROR"


class UtteranceCapturer(FrameProcessor):
    """Identical to v5's UtteranceCapturer."""

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
    """Same as v5 but uses top-level Model import instead of lazy import."""

    def __init__(self, capturer):
        super().__init__()
        print("Loading openwakeword models...")
        # CHANGE A: Model is already imported at top level — no lazy import here
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


class DeepgramSTTProcessor(FrameProcessor):
    """Identical to v5's DeepgramSTTProcessor."""

    def __init__(self, capturer):
        super().__init__()
        self.capturer = capturer
        self.dg_client = DeepgramClient()

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    def transcribe_sync(self):
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

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("Incremental v6 (v5 + import timing alignment): Listening...")
    print("   Say 'hey Jarvis', speak a short sentence, then Ctrl+C to test shutdown.")
    print("   Press Ctrl+C to test shutdown.\n")

    await runner.run(task)

    # Post-pipeline: transcribe any captured audio
    print("\nRunning final transcription...")
    stt_processor.transcribe_sync()

    print("Incremental v6 finished cleanly -- process should exit now.")


if __name__ == "__main__":
    asyncio.run(main())
