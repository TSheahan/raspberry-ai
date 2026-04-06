"""EU-3c Track 2 — Pipecat pipeline harness (single process).

Proves Pipecat processor adaptations work with RecorderState interface.
Real OWW, real Silero, real PyAudio/ReSpeaker. No fork, no SharedMemory, no Pipe.

RecorderStateStub stubs the downstream port (IPC/ring buffer) while inheriting
real Pipecat-coupled methods (_start_stream, _stop_stream, _reset_oww_full,
_clear_oww, _reset_silero) from the base class.

Usage (on Pi with ReSpeaker):
    cd forked_assistant && python track2_pipeline_harness.py
"""

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import asyncio
import signal
import time
import numpy as np
from functools import partial

from openwakeword.model import Model as OWWModel

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.frames.frames import Frame, AudioRawFrame, InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.vad.vad_controller import VADController

from recorder_state import RecorderState


# ---------------------------------------------------------------------------
# RecorderStateStub — downstream port stubbed, upstream port real
# ---------------------------------------------------------------------------

class RecorderStateStub(RecorderState):
    """RecorderState with real state machine but stubbed IPC.
    Signals are collected into self.events for inspection.
    Ring buffer writes are discarded.
    """

    def __init__(self):
        super().__init__(pipe=None, shm=None)
        self.events: list[dict] = []

    def write_audio(self, frame_bytes: bytes) -> None:
        pass  # discard — ring buffer not under test

    def signal_wake_detected(self, score: float, keyword: str) -> None:
        self.events.append({
            "cmd": "WAKE_DETECTED",
            "write_pos": self.write_pos,
            "score": score,
            "keyword": keyword,
        })
        print(f"  [STUB] WAKE_DETECTED score={score:.3f} keyword={keyword}")

    def signal_vad_started(self) -> None:
        self.events.append({"cmd": "VAD_STARTED", "write_pos": self.write_pos})
        print(f"  [STUB] VAD_STARTED write_pos={self.write_pos}")

    def signal_vad_stopped(self) -> None:
        self.events.append({"cmd": "VAD_STOPPED", "write_pos": self.write_pos})
        print(f"  [STUB] VAD_STOPPED write_pos={self.write_pos}")

    def signal_state_changed(self) -> None:
        self.events.append({"cmd": "STATE_CHANGED", "state": self.phase})
        print(f"  [STUB] STATE_CHANGED -> {self.phase}")


# ---------------------------------------------------------------------------
# GatedVADProcessor — adapted from v10a for RecorderState interface
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
            pass  # signals go through state, not Pipecat broadcast

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._vad_controller.process_frame(frame)
        elif isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            self.state.inc_total_frames()
            if self.state.capture:
                self.state.inc_vad_frames()
                if self.state.vad_frame_count % 50 == 1:
                    print(f"  [VAD] feeding frame #{self.state.vad_frame_count} to Silero "
                          f"(total: {self.state.total_frame_count})")
                await self._vad_controller.process_frame(frame)


# ---------------------------------------------------------------------------
# OpenWakeWordProcessor — adapted from v10a for RecorderState interface
# ---------------------------------------------------------------------------

class OpenWakeWordProcessor(FrameProcessor):
    """OWW processor that only runs inference during WAKE_LISTEN phase.

    Gating: when not in wake_listen, chunks are discarded and frames pass through.
    OWW full reset is handled by RecorderState.set_phase() on transition —
    the _was_gated in-processor detection from v10a is removed.
    """

    def __init__(self, state: RecorderState):
        super().__init__()
        self.state = state
        print("Loading openwakeword models...")
        self.model = OWWModel()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        print("openwakeword ready")

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            if not self.state.wake_listen:
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
                        self.state.signal_wake_detected(score, wakeword)
            remainder = buffer[consumed:]
            self._chunks = [remainder] if len(remainder) > 0 else []

        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# AudioShmRingWriteProcessor — writes audio frames via state.write_audio()
# ---------------------------------------------------------------------------

class AudioShmRingWriteProcessor(FrameProcessor):
    """Writes audio frames to the ring buffer via state.write_audio().
    In Track 2 this is a no-op (RecorderStateStub discards writes).
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
# Command driver — simulates master commands via direct state.set_phase()
# ---------------------------------------------------------------------------

async def direct_command_driver(state: RecorderStateStub):
    """Drive state transitions without a pipe — simulates master commands."""
    await asyncio.sleep(1.0)
    print("[HARNESS] Setting phase to wake_listen")
    await state.set_phase("wake_listen")

    cycles = 0
    while cycles < 3:
        await asyncio.sleep(0.1)
        if not state.events:
            continue
        last = state.events[-1]
        if last["cmd"] == "WAKE_DETECTED" and state.wake_listen:
            print("[HARNESS] Wake detected -> setting capture")
            await state.set_phase("capture")
        elif last["cmd"] == "VAD_STOPPED" and state.capture:
            cycles += 1
            print(f"[HARNESS] VAD stopped -- cycle {cycles}/3 complete "
                  f"({len(state.events)} events)")
            await state.set_phase("wake_listen")

    print(f"\n[HARNESS] 3 cycles complete. Total events: {len(state.events)}")
    await state.set_phase("dormant")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    state = RecorderStateStub()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_device_index=1,    # ReSpeaker
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
    ring_writer = AudioShmRingWriteProcessor(state=state)

    input_transport = transport.input()

    # Wire state refs
    state.set_transport(input_transport)
    state.set_vad(vad_processor)
    state.set_oww(wake_processor)
    state.set_ring_writer(ring_writer)

    pipeline = Pipeline([input_transport, vad_processor, wake_processor, ring_writer])
    runner = PipelineRunner()
    task = PipelineTask(pipeline)

    # Patch transport cancel for clean PyAudio shutdown
    original_cancel = input_transport.cancel

    async def cancel_with_stream_stop(frame):
        if hasattr(input_transport, '_in_stream') and input_transport._in_stream:
            print("  (stopping PyAudio stream before teardown)")
            input_transport._in_stream.stop_stream()
        await asyncio.sleep(0.1)
        await original_cancel(frame)

    input_transport.cancel = cancel_with_stream_stop

    print("EU-3c Track 2 -- Pipecat pipeline harness (single process)")
    print("  Say 'hey Jarvis', speak, then pause.")
    print("  3 wake->capture->VAD cycles, then exit.")
    print("  Press Ctrl+C to exit early.\n")

    loop = asyncio.get_running_loop()
    shutdown_task_ref = None

    def signal_handler(sig):
        nonlocal shutdown_task_ref
        print(f"\nReceived {sig} -- cancelling pipeline...")
        if shutdown_task_ref is None:
            shutdown_task_ref = asyncio.create_task(task.cancel())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(signal_handler, sig.name))

    driver = asyncio.create_task(direct_command_driver(state))

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        print("\nPipeline cancelled.")
    finally:
        driver.cancel()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        print("\nEU-3c harness finished.")
        print(f"Events collected: {len(state.events)}")
        for i, ev in enumerate(state.events):
            print(f"  {i}: {ev}")


if __name__ == "__main__":
    asyncio.run(main())
