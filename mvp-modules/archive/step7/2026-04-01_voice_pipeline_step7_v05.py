"""Step 7 v05 — Wake word + VAD + STT + agentic layer (claude -p).

Pipecat pipeline: ReSpeaker mic -> GatedVAD -> openWakeWord -> UtteranceCapturer -> sink.

v05 changes from v04:
  - Diagnostic prints in GatedVADProcessor: frame count to Silero, speech_started/
    speech_stopped event confirmations. Purpose: identify why VAD doesn't fire
    after wake word detection. Clean shutdown patch retained from v04.

Provenance: builds on voice_pipeline_step7_v04.py.
"""

import asyncio
import os
import sys
import time
import tempfile
import wave
import subprocess
import numpy as np
from collections import deque
from functools import partial
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
from pipecat.frames.frames import Frame, AudioRawFrame, InputAudioRawFrame, StartFrame, CancelFrame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.vad.vad_controller import VADController
from pipecat.processors.frame_processor import FrameDirection

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
# Shutdown: stop frame production before teardown
# ---------------------------------------------------------------------------

def patch_transport_cancel(input_transport):
    """Patch the input transport's cancel to stop PyAudio stream immediately."""
    original_cancel = input_transport.cancel

    async def cancel_with_stream_stop(frame):
        if input_transport._in_stream:
            print("  (stopping PyAudio stream before teardown)")
            input_transport._in_stream.stop_stream()
        await asyncio.sleep(0.1)
        await original_cancel(frame)

    input_transport.cancel = cancel_with_stream_stop


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

class GatedVADProcessor(FrameProcessor):
    """VAD that only runs Silero inference during CAPTURING phase.

    v05: added diagnostic counters and event prints.
    """

    def __init__(self, *, vad_analyzer, capturer, **kwargs):
        super().__init__(**kwargs)
        self._capturer = capturer
        self._vad_controller = VADController(vad_analyzer)
        self._vad_frame_count = 0  # DIAG: frames sent to Silero
        self._total_frame_count = 0  # DIAG: total audio frames received

        @self._vad_controller.event_handler("on_speech_started")
        async def on_speech_started(_controller):
            print(f"  [VAD] speech_started (after {self._vad_frame_count} frames to Silero)")

        @self._vad_controller.event_handler("on_speech_stopped")
        async def on_speech_stopped(_controller):
            print(f"  [VAD] speech_stopped (after {self._vad_frame_count} frames to Silero)")
            await self.broadcast_frame(
                VADUserStoppedSpeakingFrame,
                stop_secs=_controller._vad_analyzer.params.stop_secs,
            )

        # @self._vad_controller.event_handler("on_speech_activity")
        # async def on_speech_activity(_controller):
        #     pass  # Silenced — fires every 0.2s during speech, too noisy

        @self._vad_controller.event_handler("on_push_frame")
        async def on_push_frame(_controller, frame, direction):
            await self.push_frame(frame, direction)

        @self._vad_controller.event_handler("on_broadcast_frame")
        async def on_broadcast_frame(_controller, frame_cls, **kw):
            await self.broadcast_frame(frame_cls, **kw)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._vad_controller.process_frame(frame)
        elif isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            self._total_frame_count += 1
            if self._capturer.capturing:
                self._vad_frame_count += 1
                # DIAG: print every 50th frame (~1s) to confirm flow
                if self._vad_frame_count % 50 == 1:
                    print(f"  [VAD] feeding frame #{self._vad_frame_count} to Silero "
                          f"(total audio frames: {self._total_frame_count})")
                await self._vad_controller.process_frame(frame)


class OpenWakeWordProcessor(FrameProcessor):
    def __init__(self, capturer):
        super().__init__()
        print("Loading openwakeword models...")
        self.model = OWWModel()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self.capturer = capturer
        self._was_gated = False
        print("openwakeword ready")

    def _reset_model_full(self):
        """Reset all OWW internal state — prediction buffer AND preprocessor buffers."""
        self.model.reset()

        pp = self.model.preprocessor
        if hasattr(pp, 'raw_data_buffer'):
            pp.raw_data_buffer.clear()
        if hasattr(pp, 'melspectrogram_buffer'):
            pp.melspectrogram_buffer = np.zeros(pp.melspectrogram_buffer.shape,
                                                dtype=pp.melspectrogram_buffer.dtype)
        if hasattr(pp, 'feature_buffer'):
            pp.feature_buffer = np.zeros(pp.feature_buffer.shape,
                                         dtype=pp.feature_buffer.dtype)
        if hasattr(pp, 'accumulated_samples'):
            pp.accumulated_samples = 0

        self._chunks = []
        self.last_detection_time = time.time()
        print("  (OWW model state fully reset)")

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            is_gated = self.capturer.processing or self.capturer.capturing

            if self._was_gated and not is_gated:
                self._reset_model_full()

            self._was_gated = is_gated

            if is_gated:
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
            remainder = buffer[consumed:]
            self._chunks = [remainder] if len(remainder) > 0 else []
        await self.push_frame(frame, direction)


class UtteranceCapturer(FrameProcessor):
    """Captures audio between wake word and VAD stop-speaking."""

    def __init__(self):
        super().__init__()
        self._chunks = []
        self.capturing = False
        self.processing = False
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
            asyncio.create_task(self._cognitive_loop(audio))

        await self.push_frame(frame, direction)

    async def _cognitive_loop(self, audio: np.ndarray):
        """Transcribe audio, send to Claude, print response with timings."""
        loop_start = time.time()

        try:
            stt_start = time.time()
            transcript = await asyncio.to_thread(self._transcribe, audio)
            stt_elapsed = time.time() - stt_start

            if not transcript:
                print("  No transcript returned.")
                return

            print(f"  TRANSCRIPT: {transcript}")
            print(f"  STT latency: {stt_elapsed:.2f}s")

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

    capturer = UtteranceCapturer()

    vad_processor = GatedVADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                stop_secs=0.8,
                start_secs=0.2,
            ),
        ),
        capturer=capturer,
    )

    wake_processor = OpenWakeWordProcessor(capturer)

    input_transport = transport.input()
    patch_transport_cancel(input_transport)

    pipeline = Pipeline([
        input_transport,
        vad_processor,
        wake_processor,
        capturer,
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("Step 7 v05 -- Wake word + VAD + STT + Claude (with VAD diagnostics)")
    print("   Say 'hey Jarvis', speak your question, then pause.")
    print("   VAD will detect end of utterance automatically.")
    print("   Press Ctrl+C to exit.\n")
    print("Listening for wake word...")

    await runner.run(task)

    print("\nStep 7 v05 finished cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
