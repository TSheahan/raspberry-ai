"""Step 7 — Wake word + VAD + STT + agentic layer (claude -p).

Pipecat pipeline: ReSpeaker mic -> VAD -> openWakeWord -> UtteranceCapturer -> sink.
On wake word, captures utterance. On VAD stop-speaking, transcribes via Deepgram
and sends transcript to claude -p. Prints response and latency. Returns to listening.

Provenance: builds on voice_pipeline_step6.py, adds VADProcessor + run_claude().

v01a change: Gate OWW predict during capturer.capturing or capturer.processing.
OWW ONNX (20-40ms/call) competed with Claude subprocess for CPU → queue overflow.
VAD (Silero) unchanged — still runs on every frame upstream of OWW.
"""

import asyncio
import os
import sys
import time
import tempfile
import wave
import subprocess
import numpy as np
from dotenv import load_dotenv

load_dotenv(override=True)

from openwakeword.model import Model as OWWModel
from deepgram import DeepgramClient

if not os.environ.get("DEEPGRAM_API_KEY"):
    sys.exit("DEEPGRAM_API_KEY not set. Check ~/.env")

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.frames.frames import Frame, AudioRawFrame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

os.environ["ORT_LOG_LEVEL"] = "ERROR"


# ---------------------------------------------------------------------------
# Agentic layer — Option A (claude CLI on Pi)
# ---------------------------------------------------------------------------

def run_claude(transcript: str) -> str:
    """Call claude -p with the transcript. Returns response text."""
    result = subprocess.run(
        ["claude", "-p", transcript, "--model", "claude-haiku-4-5-20251001"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return f"[claude error: {result.stderr.strip()}]"
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

class OpenWakeWordProcessor(FrameProcessor):
    def __init__(self, capturer):
        super().__init__()
        print("Loading openwakeword models...")
        self.model = OWWModel()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self.capturer = capturer
        print("openwakeword ready")

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            # Skip predict while capturing or processing — CPU contention with
            # Silero VAD and the Claude subprocess causes queue accumulation.
            if self.capturer.processing or self.capturer.capturing:
                self._chunks = []
                await self.push_frame(frame, direction)
                return
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self._chunks.append(audio_chunk)
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
            self._chunks = [remainder] if len(remainder) > 0 else []
        await self.push_frame(frame, direction)


class UtteranceCapturer(FrameProcessor):
    """Captures audio between wake word and VAD stop-speaking.

    On VADUserStoppedSpeakingFrame (while capturing), runs the cognitive loop
    in a background task: transcribe -> claude -p -> print response.
    Then resets to await the next wake word.
    """

    def __init__(self):
        super().__init__()
        self._chunks = []
        self.capturing = False
        self.processing = False  # True while cognitive loop is running
        self.dg_client = DeepgramClient()

    def start_capture(self):
        if self.processing:
            print("  (still processing previous utterance, ignoring wake word)")
            return
        self.capturing = True
        self._chunks = []
        print("  Listening for utterance... (speak now)")

    def get_audio(self):
        if not self._chunks:
            return np.array([], dtype=np.int16)
        return np.concatenate(self._chunks)

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame) and self.capturing:
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self._chunks.append(audio_chunk)

        if isinstance(frame, VADUserStoppedSpeakingFrame) and self.capturing:
            self.capturing = False
            self.processing = True
            audio = self.get_audio()
            duration = len(audio) / 16000.0
            print(f"  Utterance captured: {duration:.1f}s")
            # Run cognitive loop in background so pipeline keeps flowing
            asyncio.create_task(self._cognitive_loop(audio))

        await self.push_frame(frame, direction)

    async def _cognitive_loop(self, audio: np.ndarray):
        """Transcribe audio, send to Claude, print response with timings."""
        loop_start = time.time()

        try:
            # --- STT ---
            stt_start = time.time()
            transcript = await asyncio.to_thread(self._transcribe, audio)
            stt_elapsed = time.time() - stt_start

            if not transcript:
                print("  No transcript returned.")
                return

            print(f"  TRANSCRIPT: {transcript}")
            print(f"  STT latency: {stt_elapsed:.2f}s")

            # --- Agentic layer ---
            claude_start = time.time()
            response = await asyncio.to_thread(run_claude, transcript)
            claude_elapsed = time.time() - claude_start

            print(f"\n  CLAUDE RESPONSE:\n  {response}\n")
            print(f"  Claude latency: {claude_elapsed:.2f}s")
            print(f"  Total loop latency: {time.time() - loop_start:.2f}s")
            print("\nListening for wake word...")

        except Exception as e:
            print(f"  Cognitive loop error: {e}")
        finally:
            self.processing = False

    def _transcribe(self, audio: np.ndarray) -> str:
        """Synchronous Deepgram file-based transcription."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp.name, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio.tobytes())
            tmp_path = tmp.name

        print("  Sending audio to Deepgram Nova-3...")
        try:
            with open(tmp_path, "rb") as audio_file:
                response = self.dg_client.listen.v1.media.transcribe_file(
                    request=audio_file.read(),
                    model="nova-3",
                    smart_format=True,
                    language="en",
                )
            return response.results.channels[0].alternatives[0].transcript.strip()
        except Exception as e:
            print(f"  Deepgram error: {e}")
            return ""
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,   # ReSpeaker
            audio_out_enabled=False,
        )
    )

    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                stop_secs=0.8,   # 0.8s silence = end of utterance
                start_secs=0.2,
            ),
        ),
    )

    capturer = UtteranceCapturer()
    wake_processor = OpenWakeWordProcessor(capturer)

    pipeline = Pipeline([
        transport.input(),
        vad_processor,        # VAD first — emits start/stop speaking frames
        wake_processor,       # wake word detection on all audio
        capturer,             # captures audio + reacts to VAD stop
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("Step 7 v01a -- Wake word + VAD + STT + Claude")
    print("   Say 'hey Jarvis', speak your question, then pause.")
    print("   VAD will detect end of utterance automatically.")
    print("   Press Ctrl+C to exit.\n")
    print("Listening for wake word...")

    await runner.run(task)

    print("\nStep 7 finished cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
