"""Step 6 — Wake word + utterance capture + Deepgram STT.

Pipecat pipeline: ReSpeaker mic -> openWakeWord -> utterance capture -> Deepgram Nova-3.
Ctrl+C exits cleanly. Post-pipeline batch transcription of captured audio.

Provenance: step6_worklog/incremental_v10.py (see step6_worklog/README.md for history).
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
    def __init__(self, capturer):
        super().__init__()
        print("Loading openwakeword models...")
        self.model = Model()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self.capturer = capturer
        print("openwakeword ready")

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self._chunks.append(audio_chunk)
            # Concatenate only when we have enough for a predict call
            buffer = np.concatenate(self._chunks)
            chunk_size = 1280
            consumed = 0
            while len(buffer) - consumed >= chunk_size:
                chunk = buffer[consumed:consumed + chunk_size]
                consumed += chunk_size
                predictions = self.model.predict(chunk.astype(np.float32))
                current_time = time.time()
                for wakeword, score in predictions.items():
                    if (wakeword == "hey_jarvis"
                            and score > 0.5
                            and (current_time - self.last_detection_time) > self.DEBOUNCE_SECONDS):
                        print(f"\nWAKE DETECTED -- '{wakeword}'  |  score: {score:.3f}")
                        self.last_detection_time = current_time
                        self.capturer.start_capture()
            # Keep only unconsumed remainder
            remainder = buffer[consumed:]
            if len(remainder) > 0:
                self._chunks = [remainder]
            else:
                self._chunks = []
        await self.push_frame(frame, direction)


class UtteranceCapturer(FrameProcessor):
    def __init__(self):
        super().__init__()
        self._chunks = []
        self.capturing = False

    def start_capture(self):
        self.capturing = True
        self._chunks = []

    def get_audio(self):
        """Concatenate captured chunks. Called once at transcription time."""
        if not self._chunks:
            return np.array([], dtype=np.int16)
        return np.concatenate(self._chunks)

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame) and self.capturing:
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self._chunks.append(audio_chunk)
        await self.push_frame(frame, direction)


class DeepgramSTTProcessor(FrameProcessor):
    def __init__(self, capturer):
        super().__init__()
        self.capturer = capturer
        self.dg_client = DeepgramClient()

    async def process_frame(self, frame: Frame, direction: str):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    def transcribe_sync(self):
        audio = self.capturer.get_audio()
        if len(audio) == 0:
            print("No utterance buffer to transcribe.")
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp.name, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio.tobytes())
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

    print("Step 6 v10 — Listening...")
    print("   Say 'hey Jarvis', speak a short sentence, then Ctrl+C to test shutdown.")
    print("   Press Ctrl+C to test shutdown.\n")

    await runner.run(task)

    print("\nRunning final transcription...")
    stt_processor.transcribe_sync()

    print("Step 6 v10 finished cleanly -- process should exit now.")


if __name__ == "__main__":
    asyncio.run(main())
