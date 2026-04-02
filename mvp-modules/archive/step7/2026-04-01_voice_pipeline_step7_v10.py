"""Step 7 v09 — Crash-on-exit isolation gates.

Crash only manifests when VAD stopped-speaking fires (VADUserStoppedSpeakingFrame
reaches UtteranceCapturer.process_frame). v09 adds four boolean globals that
progressively gate the suspect logic so we can binary-search the crash site:

  GATE_VAD_ALL          — Gate 1: skip self.get_audio() and all downstream in
                          the VADUserStoppedSpeakingFrame block.
  GATE_VAD_CREATE_TASK  — Gate 2: skip only the asyncio.create_task() call
                          (get_audio still runs, cognitive loop does not).
  GATE_COGNITIVE_STT    — Gate 3a: skip asyncio.to_thread(self._transcribe, ...)
                          inside _cognitive_loop.
  GATE_COGNITIVE_CLAUDE — Gate 3b: skip asyncio.to_thread(run_claude, ...)
                          inside _cognitive_loop.

Procedure: set one gate True at a time and re-run; if the crash disappears the
culprit is inside that gate. All gates default to False (normal behaviour).

Provenance: builds on voice_pipeline_step7_v08.py.
"""

import asyncio
import os
import sys
import time
import tempfile
import wave
import subprocess
import numpy as np
import weakref
import signal
from functools import partial
from collections import deque
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

TRACE_HANDLER_EXCEPTIONS = True

# ---------------------------------------------------------------------------
# Crash-isolation gates — toggle one at a time to narrow the crash site.
# All False = normal v08 behaviour.
# ---------------------------------------------------------------------------

# Gate 1: skip self.get_audio() and everything downstream in the
#         VADUserStoppedSpeakingFrame block of UtteranceCapturer.process_frame.
GATE_VAD_ALL = False

# Gate 2: skip only asyncio.create_task(_cognitive_loop) — get_audio still runs.
#         Only consulted when GATE_VAD_ALL is False.
GATE_VAD_CREATE_TASK = False

# Gate 3a: skip asyncio.to_thread(self._transcribe, ...) inside _cognitive_loop.
#          Only consulted when GATE_VAD_CREATE_TASK is False.
GATE_COGNITIVE_STT = False

# Gate 3b: skip asyncio.to_thread(run_claude, ...) inside _cognitive_loop.
#          Only consulted when GATE_VAD_CREATE_TASK is False.
GATE_COGNITIVE_CLAUDE = False

# Gate 4: skip self._vad_controller.process_frame(frame) for audio frames
#         during CAPTURING phase inside GatedVADProcessor.process_frame.
#         Confirmed crash survives GATE_VAD_ALL=True, so culprit is upstream
#         of UtteranceCapturer — this gate isolates the VAD controller itself.
GATE_VAD_CONTROLLER = False


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
    """VAD that only runs Silero inference during CAPTURING phase."""

    def __init__(self, *, vad_analyzer, capturer, **kwargs):
        super().__init__(**kwargs)
        self._capturer = capturer
        self._vad_analyzer = vad_analyzer
        self._vad_controller = VADController(vad_analyzer)
        self._vad_frame_count = 0
        self._total_frame_count = 0

        if TRACE_HANDLER_EXCEPTIONS:
            @self._vad_controller.event_handler("on_speech_started")
            async def on_speech_started(_controller):
                try:
                    print(f"  [VAD] speech_started (after {self._vad_frame_count} frames to Silero)")
                except Exception as e:
                    print(f"  [VAD] on_speech_started {e}")
                    raise

            @self._vad_controller.event_handler("on_speech_stopped")
            async def on_speech_stopped(_controller):
                try:
                    print(f"  [VAD] speech_stopped (after {self._vad_frame_count} frames to Silero)")
                    await self.broadcast_frame(
                        VADUserStoppedSpeakingFrame,
                        stop_secs=_controller._vad_analyzer.params.stop_secs,
                    )
                except Exception as e:
                    print(f"  [VAD] on_speech_stopped {e}")
                    raise

            @self._vad_controller.event_handler("on_push_frame")
            async def on_push_frame(_controller, frame, direction):
                try:
                    await self.push_frame(frame, direction)
                except Exception as e:
                    print(f"  [VAD] on_push_frame {e}")
                    raise

            @self._vad_controller.event_handler("on_broadcast_frame")
            async def on_broadcast_frame(_controller, frame_cls, **kw):
                try:
                    await self.broadcast_frame(frame_cls, **kw)
                except Exception as e:
                    print(f"  [VAD] on_broadcast_frame {e}")
                    raise
        else:
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

            @self._vad_controller.event_handler("on_push_frame")
            async def on_push_frame(_controller, frame, direction):
                await self.push_frame(frame, direction)

            @self._vad_controller.event_handler("on_broadcast_frame")
            async def on_broadcast_frame(_controller, frame_cls, **kw):
                await self.broadcast_frame(frame_cls, **kw)


    async def reset_vad(self):
        """Reset Silero LSTM hidden states so the next utterance starts fresh."""
        if hasattr(self._vad_analyzer, "_model") and hasattr(self._vad_analyzer._model, "reset_states"):
            self._vad_analyzer._model.reset_states()
            self._vad_frame_count = 0
            print("  [VAD] Silero model states reset (ready for next utterance)")
        else:
            print("  [VAD] WARNING: could not find _model.reset_states() — check Pipecat version")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._vad_controller.process_frame(frame)
        elif isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            if GATE_VAD_CONTROLLER:
                pass  # this gate suppresses all process_frame
            else:
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
        self._vad_weak = None

    def start_capture(self):
        if self.processing:
            print("  (still processing previous utterance, ignoring wake word)")
            return

        # Extra safety: reset VAD the instant wake word is detected
        if self._vad_weak:
            vad = self._vad_weak()
            if vad is not None:
                asyncio.create_task(vad.reset_vad())

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

        # --- second conditional: VAD stopped-speaking path ---
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self.capturing:
            self.capturing = False
            self.processing = True

            # Gate 1: skip self.get_audio() and all downstream logic.
            if GATE_VAD_ALL:
                print("  [GATE_VAD_ALL] get_audio + downstream suppressed; clearing processing flag")
                self.processing = False
            else:
                audio = self.get_audio()
                duration = len(audio) / 16000.0
                print(f"  Utterance captured: {duration:.1f}s")

                # Gate 2: skip asyncio.create_task only.
                if GATE_VAD_CREATE_TASK:
                    print("  [GATE_VAD_CREATE_TASK] create_task suppressed; clearing processing flag")
                    self.processing = False
                else:
                    asyncio.create_task(self._cognitive_loop(audio))

        await self.push_frame(frame, direction)

    async def _cognitive_loop(self, audio: np.ndarray):
        """Transcribe audio, send to Claude, print response with timings."""
        loop_start = time.time()

        try:
            # Gate 3a: skip STT.
            if GATE_COGNITIVE_STT:
                print("  [GATE_COGNITIVE_STT] STT suppressed")
                transcript = "[GATED-STT]"
                stt_elapsed = 0.0
            else:
                stt_start = time.time()
                transcript = await asyncio.to_thread(self._transcribe, audio)
                stt_elapsed = time.time() - stt_start

            if not transcript:
                print("  No transcript returned.")
                return

            print(f"  TRANSCRIPT: {transcript}")
            if not GATE_COGNITIVE_STT:
                print(f"  STT latency: {stt_elapsed:.2f}s")

            # Gate 3b: skip Claude.
            if GATE_COGNITIVE_CLAUDE:
                print("  [GATE_COGNITIVE_CLAUDE] Claude suppressed")
                response = "[GATED-CLAUDE]"
                claude_elapsed = 0.0
            else:
                claude_start = time.time()
                response = await asyncio.to_thread(run_claude, transcript)
                claude_elapsed = time.time() - claude_start

            print(f"\n  CLAUDE RESPONSE:\n  {response}\n")
            if not GATE_COGNITIVE_CLAUDE:
                print(f"  Claude latency: {claude_elapsed:.2f}s")
            print(f"  Total loop latency: {time.time() - loop_start:.2f}s")
            print("\nListening for wake word...")

        except Exception as e:
            print(f"  Cognitive loop error: {e}")
        finally:
            self.processing = False
            if self._vad_weak:
                vad = self._vad_weak()
                if vad is not None:
                    await vad.reset_vad()
            print("\nListening for wake word...")

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
                stop_secs=1.8,
                start_secs=0.2,
            ),
        ),
        capturer=capturer,
    )

    # avoiding a circular ref
    capturer._vad_weak = weakref.ref(vad_processor)

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

    print("Step 7 v09 — crash-on-exit isolation gates active")
    print(f"  GATE_VAD_CONTROLLER={GATE_VAD_CONTROLLER}  GATE_VAD_ALL={GATE_VAD_ALL}  GATE_VAD_CREATE_TASK={GATE_VAD_CREATE_TASK}")
    print(f"  GATE_COGNITIVE_STT={GATE_COGNITIVE_STT}  GATE_COGNITIVE_CLAUDE={GATE_COGNITIVE_CLAUDE}")
    print("   Say 'hey Jarvis', speak your question, then pause.")
    print("   VAD will detect end of utterance automatically.")
    print("   Press Ctrl+C (or send SIGTERM) to exit.\n")
    print("Listening for wake word...")

    loop = asyncio.get_running_loop()
    shutdown_task = None

    def signal_handler(sig):
        nonlocal shutdown_task
        print(f"\nReceived {sig} — cancelling pipeline gracefully...")
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(shutdown())

    async def shutdown():
        await task.cancel()
        await asyncio.sleep(0.5)
        print("  (pipeline cancelled cleanly via patch_transport_cancel)")

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(signal_handler, sig.name))

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        print("\nPipeline was cancelled cleanly.")
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        print("\nStep 7 v09 finished cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
