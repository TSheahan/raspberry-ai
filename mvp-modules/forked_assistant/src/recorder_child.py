"""
recorder_child.py — Complete recorder subprocess (EU-3d merge).

Runs in a forked child process pinned to core 0. Owns the microphone via
Pipecat + PyAudio, runs OWW wake word detection and Silero VAD, writes
audio to a SharedMemory ring buffer, and sends events over a Pipe to the
master process.

Combines Track 1's real downstream port (ring buffer + pipe signals) with
Track 2's real Pipecat pipeline (GatedVADProcessor, OpenWakeWordProcessor).

Entry point: recorder_child_entry(pipe, shm_name) — used as
multiprocessing.Process target by the master.
"""

import asyncio
import os
import signal
import sys
import time

import numpy as np
from collections import deque
from functools import partial
from multiprocessing.shared_memory import SharedMemory

from openwakeword.model import Model as OWWModel

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import (
    LocalAudioTransport, LocalAudioTransportParams,
)
from pipecat.frames.frames import (
    Frame, AudioRawFrame, InputAudioRawFrame, StartFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.vad.vad_controller import VADController

from recorder_state import RecorderState
from ring_buffer import RingBufferWriter


# ---------------------------------------------------------------------------
# RecorderChild — real downstream port (ring + pipe), real upstream port
# ---------------------------------------------------------------------------

class RecorderChild(RecorderState):
    """RecorderState with real ring-buffer writes and real pipe signals.

    Pipecat-coupled methods (_start_stream, _stop_stream, _reset_oww_full,
    _clear_oww, _reset_silero, _drain_oww_predict) are inherited from the
    base class — they use the weakref wiring set up during pipeline init.
    """

    def __init__(self, pipe, ring_writer: RingBufferWriter):
        super().__init__(pipe=pipe, shm=None)
        self._ring_writer = ring_writer

    # --- Audio write (→ SharedMemory ring buffer) ---

    def write_audio(self, frame_bytes: bytes) -> None:
        self._ring_writer.write(frame_bytes)
        self._write_pos = self._ring_writer.write_pos

    # --- Signal emission (→ pipe to master) ---

    def signal_wake_detected(self, score: float, keyword: str) -> None:
        self._pipe.send({
            "cmd": "WAKE_DETECTED",
            "write_pos": self._write_pos,
            "score": score,
            "keyword": keyword,
        })

    def signal_vad_started(self) -> None:
        self._pipe.send({"cmd": "VAD_STARTED", "write_pos": self._write_pos})

    def signal_vad_stopped(self) -> None:
        self._pipe.send({"cmd": "VAD_STOPPED", "write_pos": self._write_pos})

    def signal_state_changed(self) -> None:
        self._pipe.send({"cmd": "STATE_CHANGED", "state": self._phase})


# ---------------------------------------------------------------------------
# GatedVADProcessor — Silero inference gated to CAPTURE phase only
# ---------------------------------------------------------------------------

class GatedVADProcessor(FrameProcessor):
    """VAD that only runs Silero inference during CAPTURE phase.

    Selective conditional: only StartFrame and audio-during-capture reach
    the VAD controller. CancelFrame/EndFrame never reach it (crash fix).
    """

    def __init__(self, *, vad_analyzer, state: RecorderState, **kwargs):
        super().__init__(**kwargs)
        self.state = state
        self._vad_analyzer = vad_analyzer
        self._vad_controller = VADController(vad_analyzer)

        @self._vad_controller.event_handler("on_speech_started")
        async def on_speech_started(_controller):
            print(f"  [VAD] speech_started (after {self.state.vad_frame_count} frames)")
            self.state.signal_vad_started()

        @self._vad_controller.event_handler("on_speech_stopped")
        async def on_speech_stopped(_controller):
            print(f"  [VAD] speech_stopped (after {self.state.vad_frame_count} frames)")
            self.state.signal_vad_stopped()

        @self._vad_controller.event_handler("on_push_frame")
        async def on_push_frame(_controller, frame, direction):
            await self.push_frame(frame, direction)

        @self._vad_controller.event_handler("on_broadcast_frame")
        async def on_broadcast_frame(_controller, frame_cls, **kw):
            pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._vad_controller.process_frame(frame)
        elif isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            self.state.inc_total_frames()
            if self.state.capture:
                self.state.inc_vad_frames()
                await self._vad_controller.process_frame(frame)


# ---------------------------------------------------------------------------
# OpenWakeWordProcessor — OWW inference gated to WAKE_LISTEN phase only
# ---------------------------------------------------------------------------

class OpenWakeWordProcessor(FrameProcessor):
    """OWW processor that only runs inference during WAKE_LISTEN phase.

    Predict runs via asyncio.to_thread() so the event loop is never blocked
    by ONNX inference. Frames are pushed downstream before predict fires.
    A drain guard in RecorderState.set_phase() awaits _pending_predict on
    wake_listen->capture to prevent concurrent ONNX (OWW + Silero).
    """

    def __init__(self, state: RecorderState):
        super().__init__()
        self.state = state
        print("Loading openwakeword models...")
        self.model = OWWModel()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self._predict_times: deque[float] = deque(maxlen=500)
        self._predict_count: int = 0
        self._frames_in_wake: int = 0
        self._pending_predict: asyncio.Task | None = None
        print("openwakeword ready")

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        chunks_to_predict: list[np.ndarray] = []

        if isinstance(frame, AudioRawFrame):
            if not self.state.wake_listen:
                self._chunks = []
                await self.push_frame(frame, direction)
                return

            self._frames_in_wake += 1
            audio_chunk = np.frombuffer(frame.audio, dtype=np.int16)
            self._chunks.append(audio_chunk)
            buffer = np.concatenate(self._chunks)
            chunk_size = 1280
            consumed = 0
            while len(buffer) - consumed >= chunk_size:
                chunk = buffer[consumed:consumed + chunk_size]
                consumed += chunk_size
                chunks_to_predict.append(chunk.astype(np.float32))
            remainder = buffer[consumed:]
            self._chunks = [remainder] if len(remainder) > 0 else []

        await self.push_frame(frame, direction)

        if chunks_to_predict and self.state.wake_listen:
            if self._pending_predict and not self._pending_predict.done():
                await self._pending_predict
            self._pending_predict = asyncio.create_task(
                self._predict_async(chunks_to_predict)
            )

    async def _predict_async(self, chunks: list[np.ndarray]) -> None:
        for chunk in chunks:
            if not self.state.wake_listen:
                return
            t_pred = time.perf_counter()
            predictions = await asyncio.to_thread(self.model.predict, chunk)
            self._predict_times.append((time.perf_counter() - t_pred) * 1000.0)
            self._predict_count += 1
            if not self.state.wake_listen:
                return
            current_time = time.time()
            for wakeword, score in predictions.items():
                if (wakeword == "hey_jarvis"
                        and score > 0.5
                        and (current_time - self.last_detection_time)
                            > self.DEBOUNCE_SECONDS):
                    print(f"\nWAKE DETECTED -- '{wakeword}'  |  score: {score:.3f}")
                    self.last_detection_time = current_time
                    self.state.signal_wake_detected(score, wakeword)


# ---------------------------------------------------------------------------
# AudioFrameWriter — writes audio frames via state.write_audio()
# ---------------------------------------------------------------------------

class AudioFrameWriter(FrameProcessor):
    """Pipeline tail: writes every audio frame to the ring buffer.

    Delegates to state.write_audio(), which performs the SharedMemory
    memcpy and advances write_pos. Skips writes in DORMANT phase.
    """

    def __init__(self, state: RecorderState):
        super().__init__()
        self.state = state

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame) and not self.state.dormant:
            self.state.write_audio(frame.audio)
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# command_listener — routes pipe commands to state.set_phase()
# ---------------------------------------------------------------------------

async def command_listener(state: RecorderChild, pipe, pipeline_task) -> None:
    """Poll pipe for master commands; call set_phase() on each.

    Returns (and cancels the pipeline) on SHUTDOWN.
    """
    while True:
        if pipe.poll(0):
            msg = pipe.recv()
            cmd = msg.get("cmd")
            if cmd == "SET_WAKE_LISTEN":
                await state.set_phase("wake_listen")
            elif cmd == "SET_CAPTURE":
                await state.set_phase("capture")
            elif cmd == "SET_DORMANT":
                await state.set_phase("dormant")
            elif cmd == "SHUTDOWN":
                print("  [child] SHUTDOWN received")
                await state.set_phase("dormant")
                await pipeline_task.cancel()
                return
        await asyncio.sleep(0.010)


# ---------------------------------------------------------------------------
# Child process async main
# ---------------------------------------------------------------------------

async def recorder_child_main(pipe, shm_name: str) -> None:
    shm = SharedMemory(name=shm_name, create=False)
    try:
        ring_writer = RingBufferWriter(shm)
        state = RecorderChild(pipe=pipe, ring_writer=ring_writer)

        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=True,
                audio_in_device_index=1,
                audio_out_enabled=False,
            )
        )

        vad_processor = GatedVADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(stop_secs=1.8, start_secs=0.2),
            ),
            state=state,
        )

        wake_processor = OpenWakeWordProcessor(state=state)
        audio_writer = AudioFrameWriter(state=state)

        input_transport = transport.input()

        state.set_transport(input_transport)
        state.set_vad(vad_processor)
        state.set_oww(wake_processor)
        state.set_ring_writer(audio_writer)

        original_cancel = input_transport.cancel
        async def cancel_with_stream_stop(frame):
            if hasattr(input_transport, '_in_stream') and input_transport._in_stream:
                print("  [child] stopping PyAudio stream before teardown")
                input_transport._in_stream.stop_stream()
            await asyncio.sleep(0.1)
            await original_cancel(frame)
        input_transport.cancel = cancel_with_stream_stop

        pipeline = Pipeline([
            input_transport, vad_processor, wake_processor, audio_writer,
        ])
        runner = PipelineRunner()
        task = PipelineTask(pipeline)

        pipe.send({"cmd": "READY"})

        loop = asyncio.get_running_loop()
        shutdown_via_signal = False

        def on_signal(sig):
            nonlocal shutdown_via_signal
            if not shutdown_via_signal:
                shutdown_via_signal = True
                print(f"\n  [child] {sig} — cancelling pipeline...")
                asyncio.create_task(task.cancel())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, partial(on_signal, sig.name))

        listener = asyncio.create_task(command_listener(state, pipe, task))

        try:
            await runner.run(task)
        except asyncio.CancelledError:
            pass
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    finally:
        shm.close()
        print("  [child] exiting")


# ---------------------------------------------------------------------------
# Process entry point (multiprocessing.Process target)
# ---------------------------------------------------------------------------

def recorder_child_entry(pipe, shm_name: str) -> None:
    """Pin to core 0, then run the async recorder child main loop."""
    os.sched_setaffinity(0, {0})
    asyncio.run(recorder_child_main(pipe, shm_name))
