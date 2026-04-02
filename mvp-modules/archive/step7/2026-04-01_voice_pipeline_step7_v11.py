"""Step 7 v11 — PipelineState object + stream gating during processing.

v11 changes from v09/v10:
  - PipelineState: single source of truth for session phase (idle/capturing/
    processing), frame counters, and cross-object coordination via weakrefs.
  - All processor cross-references eliminated — processors hold only a ref
    to PipelineState, never to each other.
  - Stream pause/resume policy centralised in set_phase():
      processing → pauses PyAudio input stream (prevents buffer overflow
                   during CPU-heavy Claude subprocess)
      idle       → resumes stream, resets Silero hidden states + frame counter
  - Selective VAD conditional: only StartFrame + audio-during-capture reach
    the VAD controller.  CancelFrame/EndFrame never reach it (crash fix).
  - Debug gate globals retained for continued crash isolation.

Provenance: builds on voice_pipeline_step7_v09.py + v10 selective conditional.
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
# All False = normal behaviour.
# ---------------------------------------------------------------------------

GATE_VAD_ALL = False
GATE_VAD_CREATE_TASK = False
GATE_COGNITIVE_STT = False
GATE_COGNITIVE_CLAUDE = False
GATE_VAD_CONTROLLER = False


# ---------------------------------------------------------------------------
# PipelineState — shared session state
# ---------------------------------------------------------------------------

class PipelineState:
    """Single source of truth for session phase, counters, and cross-object
    coordination.  Processors read state; only state manages transitions.

    Phase lifecycle:  idle → capturing → processing → idle
    """

    def __init__(self):
        self._phase = "idle"
        self._vad_frame_count = 0
        self._total_frame_count = 0
        self._transport_ref = None      # strong ref (long-lived, no cycle)
        self._vad_ref = None            # weakref → GatedVADProcessor
        self._capturer_ref = None       # weakref → UtteranceCapturer

    # --- wiring (called once in main, after all objects exist) -------------

    def set_transport(self, input_transport):
        self._transport_ref = input_transport

    def set_vad(self, vad_processor):
        self._vad_ref = weakref.ref(vad_processor)

    def set_capturer(self, capturer):
        self._capturer_ref = weakref.ref(capturer)

    # --- read-only properties ---------------------------------------------

    @property
    def phase(self):
        return self._phase

    @property
    def capturing(self):
        return self._phase == "capturing"

    @property
    def processing(self):
        return self._phase == "processing"

    @property
    def vad_frame_count(self):
        return self._vad_frame_count

    @property
    def total_frame_count(self):
        return self._total_frame_count

    # --- phase transitions ------------------------------------------------

    def set_phase(self, phase):
        old = self._phase
        if old == phase:
            return
        self._phase = phase
        print(f"  [state] {old} -> {phase}")

        # Policy: audio stream management + VAD reset
        # All stream ops are scheduled as tasks — stop_stream() must never be
        # called synchronously from within a Pipecat frame-processing callback
        # (PortAudio deadlock / USB fault on Pi).
        if phase == "processing":
            asyncio.create_task(self._do_pause_stream())
        elif phase == "idle" and old == "processing":
            self._vad_frame_count = 0
            # Reset Silero *then* resume stream — ordering matters.
            asyncio.create_task(self._do_reset_then_resume())

        if phase == "capturing":
            self._vad_frame_count = 0
            asyncio.create_task(self._do_vad_reset())

    def request_capture(self):
        """Called by OpenWakeWordProcessor on wake-word detection."""
        if self._phase == "processing":
            print("  [state] ignoring wake word, still processing")
            return
        capturer = self._capturer_ref() if self._capturer_ref else None
        if capturer:
            capturer.clear_chunks()
        self.set_phase("capturing")
        print("  Listening for utterance... (speak now)")

    # --- frame counters ---------------------------------------------------

    def inc_total_frames(self):
        self._total_frame_count += 1
        return self._total_frame_count

    def inc_vad_frames(self):
        self._vad_frame_count += 1
        return self._vad_frame_count

    # --- internal policies (all async — never called directly from callbacks) -

    async def _do_pause_stream(self):
        if self._transport_ref and hasattr(self._transport_ref, '_in_stream') and self._transport_ref._in_stream:
            try:
                self._transport_ref._in_stream.stop_stream()
                print("  [state] audio stream paused")
            except Exception as e:
                print(f"  [state] stream pause failed: {e}")
        else:
            print(f"  [state] stream pause skip - falsy _in_stream")

    async def _do_vad_reset(self):
        vad = self._vad_ref() if self._vad_ref else None
        if vad is not None:
            await vad.reset_vad()

    async def _do_reset_then_resume(self):
        """Reset Silero hidden states BEFORE resuming the audio stream."""
        await self._do_vad_reset()
        if self._transport_ref and hasattr(self._transport_ref, '_in_stream') and self._transport_ref._in_stream:
            try:
                self._transport_ref._in_stream.start_stream()
                print("  [state] audio stream resumed")
            except Exception as e:
                print(f"  [state] stream resume failed: {e}")
        else:
            print(f"  [state] stream resume skip - falsy _in_stream")


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

    Selective conditional: only StartFrame and audio-during-capture reach the
    VAD controller.  CancelFrame / EndFrame are never forwarded (crash fix).
    """

    def __init__(self, *, vad_analyzer, state, **kwargs):
        super().__init__(**kwargs)
        self.state = state
        self._vad_analyzer = vad_analyzer
        self._vad_controller = VADController(vad_analyzer)

        if TRACE_HANDLER_EXCEPTIONS:
            @self._vad_controller.event_handler("on_speech_started")
            async def on_speech_started(_controller):
                try:
                    print(f"  [VAD] speech_started (after {self.state.vad_frame_count} frames to Silero)")
                except Exception as e:
                    print(f"  [VAD] on_speech_started {e}")
                    raise

            @self._vad_controller.event_handler("on_speech_stopped")
            async def on_speech_stopped(_controller):
                try:
                    print(f"  [VAD] speech_stopped (after {self.state.vad_frame_count} frames to Silero)")
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
                print(f"  [VAD] speech_started (after {self.state.vad_frame_count} frames to Silero)")

            @self._vad_controller.event_handler("on_speech_stopped")
            async def on_speech_stopped(_controller):
                print(f"  [VAD] speech_stopped (after {self.state.vad_frame_count} frames to Silero)")
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
            print("  [VAD] Silero model states reset (ready for next utterance)")
        else:
            print("  [VAD] WARNING: could not find _model.reset_states() — check Pipecat version")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        # Selective conditional: only StartFrame + audio-during-capture reach
        # the VAD controller.  All other frame types (CancelFrame, EndFrame,
        # etc.) are never forwarded — this prevents crash-on-exit.
        if isinstance(frame, StartFrame):
            await self._vad_controller.process_frame(frame)
        elif isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            self.state.inc_total_frames()
            if self.state.capturing:
                if GATE_VAD_CONTROLLER:
                    pass  # gate: do not feed frame to Silero
                else:
                    count = self.state.inc_vad_frames()
                    if count % 50 == 1:
                        print(f"  [VAD] feeding frame #{count} to Silero "
                              f"(total audio frames: {self.state.total_frame_count})")
                    await self._vad_controller.process_frame(frame)


class OpenWakeWordProcessor(FrameProcessor):
    def __init__(self, state):
        super().__init__()
        print("Loading openwakeword models...")
        self.model = OWWModel()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self.state = state
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
            is_gated = self.state.processing or self.state.capturing

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
                        self.state.request_capture()
            remainder = buffer[consumed:]
            self._chunks = [remainder] if len(remainder) > 0 else []
        await self.push_frame(frame, direction)


class UtteranceCapturer(FrameProcessor):
    """Captures audio between wake word and VAD stop-speaking."""

    def __init__(self, state):
        super().__init__()
        self._chunks = []
        self.state = state
        self.dg_client = DeepgramClient()

    def clear_chunks(self):
        """Called by PipelineState on wake-word detection."""
        self._chunks = []

    def get_audio(self):
        if not self._chunks:
            return np.array([], dtype=np.int16)
        return np.concatenate(self._chunks)

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame) and self.state.capturing:
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self._chunks.append(audio_chunk)

        # --- VAD stopped-speaking path ---
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self.state.capturing:
            if GATE_VAD_ALL:
                print("  [GATE_VAD_ALL] get_audio + downstream suppressed")
                self.state.set_phase("idle")
            else:
                audio = self.get_audio()
                duration = len(audio) / 16000.0
                print(f"  Utterance captured: {duration:.1f}s")

                if GATE_VAD_CREATE_TASK:
                    print("  [GATE_VAD_CREATE_TASK] create_task suppressed")
                    self.state.set_phase("idle")
                else:
                    self.state.set_phase("processing")
                    asyncio.create_task(self._cognitive_loop(audio))

        await self.push_frame(frame, direction)

    async def _cognitive_loop(self, audio: np.ndarray):
        """Transcribe audio, send to Claude, print response with timings."""
        loop_start = time.time()

        try:
            if GATE_COGNITIVE_STT:
                print("  [GATE_COGNITIVE_STT] STT suppressed")
                transcript = "[GATED-STT]"
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

            if GATE_COGNITIVE_CLAUDE:
                print("  [GATE_COGNITIVE_CLAUDE] Claude suppressed")
                response = "[GATED-CLAUDE]"
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
            self.state.set_phase("idle")
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
    state = PipelineState()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,   # ReSpeaker
            audio_out_enabled=False,
        )
    )

    capturer = UtteranceCapturer(state=state)

    vad_processor = GatedVADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                stop_secs=1.8,
                start_secs=0.2,
            ),
        ),
        state=state,
    )

    wake_processor = OpenWakeWordProcessor(state=state)

    input_transport = transport.input()
    patch_transport_cancel(input_transport)

    # Wire state refs (after all objects exist)
    state.set_transport(input_transport)
    state.set_vad(vad_processor)
    state.set_capturer(capturer)

    pipeline = Pipeline([
        input_transport,
        vad_processor,
        wake_processor,
        capturer,
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    print("Step 7 v11 — PipelineState + stream gating during processing")
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
        print("\nStep 7 v11 finished cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
