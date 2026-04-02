"""EU-3c Track 2 — Pipecat pipeline harness (single process).

Proves Pipecat processor adaptations work with RecorderState interface.
Real OWW, real Silero, real PyAudio/ReSpeaker. No fork, no SharedMemory, no Pipe.

RecorderStateStub stubs the downstream port (IPC/ring buffer) while inheriting
real Pipecat-coupled methods (_start_stream, _stop_stream, _reset_oww_full,
_clear_oww, _reset_silero) from the base class.

The InProcessRingBuffer in RecorderStateStub is sized and written identically
to the real SharedMemory ring (512 KB, wrap-around bytearray copy) so that
write_audio() imposes the same per-frame memcpy cost as production. Data
consumption is not under test here — no reader is attached.

DutyCycleProbe (optional, composed into pipeline when ENABLE_DUTY_CYCLE=1):
  Measures end-to-end pipeline processing time per audio frame by exploiting
  pipecat's direct-mode synchronous push_frame chain. Also tracks inter-frame
  arrival jitter and transport audio queue depth. Stats are printed every
  DUTY_CYCLE_WINDOW frames with start/end phase labels, and a per-phase
  histogram is printed at harness exit.

Usage (on Pi with ReSpeaker):
    cd ~/raspberry-ai/mvp-modules/forked_assistant
    source ~/pipecat-agent/venv/bin/activate
    python test/track2_pipeline_harness.py                   # without duty cycle
    ENABLE_DUTY_CYCLE=1 python test/track2_pipeline_harness.py  # with duty cycle
"""

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import asyncio
import signal
import time
import math
import numpy as np
from collections import defaultdict
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
# Ring buffer constants — match interface_spec.md / real SharedMemory ring
# ---------------------------------------------------------------------------

RING_SIZE = 524288   # 512 KB ≈ 16.4 s at 16kHz int16 mono


# ---------------------------------------------------------------------------
# InProcessRingBuffer — write-path simulation only
# ---------------------------------------------------------------------------

class InProcessRingBuffer:
    """In-process ring buffer that mirrors the SharedMemory write path.

    Exists solely to impose a realistic per-frame memcpy cost on the audio
    path. The buffer is sized identically to the real SharedMemory ring so
    the copy characteristics match production.

    Single-writer, no reader — data consumption is not under test here.
    """

    def __init__(self, size: int = RING_SIZE):
        self._buf       = bytearray(size)
        self._size      = size
        self._write_pos = 0   # monotonic byte offset

    def write(self, frame_bytes: bytes) -> None:
        n      = len(frame_bytes)
        offset = self._write_pos % self._size
        if offset + n <= self._size:
            self._buf[offset:offset + n] = frame_bytes
        else:
            first = self._size - offset
            self._buf[offset:self._size]  = frame_bytes[:first]
            self._buf[0:n - first]        = frame_bytes[first:]
        self._write_pos += n

    @property
    def write_pos(self) -> int:
        return self._write_pos

    def summary(self) -> str:
        laps = self._write_pos / self._size
        return f"ring: write_pos={self._write_pos} ({laps:.2f} laps of {self._size} B)"


# ---------------------------------------------------------------------------
# DutyCycleProbe — end-to-end pipeline timing (Methods A+B+C)
# ---------------------------------------------------------------------------

DUTY_CYCLE_WINDOW  = 100   # frames per periodic report (100 frames ≈ 2 s)
FRAME_DURATION_MS  = 20.0  # PyAudio cadence — the budget

HISTOGRAM_EDGES_MS = (0, 5, 10, 15, 20)  # bucket boundaries for final summary


class DutyCycleProbe(FrameProcessor):
    """Head-of-chain probe measuring total downstream processing time.

    Placed between the input transport and the first real processor.  In
    pipecat's direct mode, push_frame() blocks until the entire downstream
    chain returns, so timing that single call gives the true per-frame
    pipeline duty cycle.

    Also records inter-frame arrival interval (jitter) and samples the
    transport's _audio_in_queue depth each frame.
    """

    def __init__(self, state: RecorderState, transport_input=None):
        super().__init__()
        self._state = state
        self._transport_input = transport_input

        # --- rolling window (periodic report) ---
        self._window: list[float] = []
        self._window_start_phase: str = "dormant"
        self._window_arrivals: list[float] = []
        self._window_max_qdepth: int = 0

        # --- per-phase accumulators (final summary) ---
        self._phase_samples: dict[str, list[float]] = defaultdict(list)

        # --- inter-frame tracking ---
        self._last_arrival: float = 0.0
        self._total_frames: int = 0

    # -- helpers --------------------------------------------------------

    def _queue_depth(self) -> int:
        t = self._transport_input
        if t and hasattr(t, '_audio_in_queue'):
            return t._audio_in_queue.qsize()
        return -1

    @staticmethod
    def _percentile(sorted_vals: list[float], p: float) -> float:
        if not sorted_vals:
            return 0.0
        k = (len(sorted_vals) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_vals[int(k)]
        return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)

    def _emit_periodic(self) -> None:
        if not self._window:
            return
        s = sorted(self._window)
        n = len(s)
        mean  = sum(s) / n
        p95   = self._percentile(s, 0.95)
        mx    = s[-1]
        util  = mean / FRAME_DURATION_MS * 100.0

        end_phase = self._state.phase
        if self._window_start_phase == end_phase:
            phase_lbl = end_phase
        else:
            phase_lbl = f"{self._window_start_phase}->{end_phase}"

        arrival_str = ""
        if self._window_arrivals:
            sa = sorted(self._window_arrivals)
            a_mean = sum(sa) / len(sa)
            a_jitter = sa[-1] - sa[0]
            arrival_str = f"  arrival: mean={a_mean:.1f}ms jitter={a_jitter:.1f}ms"

        qdepth_str = ""
        if self._window_max_qdepth >= 0:
            qdepth_str = f"  q_max={self._window_max_qdepth}"

        print(f"  [DUTY/{self._total_frames}] {phase_lbl}: "
              f"mean={mean:.1f}ms p95={p95:.1f}ms max={mx:.1f}ms "
              f"util={util:.0f}%{arrival_str}{qdepth_str}")

        self._window.clear()
        self._window_arrivals.clear()
        self._window_max_qdepth = 0
        self._window_start_phase = self._state.phase

    def print_final_summary(self) -> None:
        print("\n" + "=" * 64)
        print("DUTY CYCLE SUMMARY")
        print("=" * 64)

        for phase in ("wake_listen", "capture", "dormant"):
            samples = self._phase_samples.get(phase)
            if not samples:
                continue
            s = sorted(samples)
            n = len(s)
            mean = sum(s) / n
            p95  = self._percentile(s, 0.95)
            p99  = self._percentile(s, 0.99)
            mx   = s[-1]
            util = mean / FRAME_DURATION_MS * 100.0

            print(f"\n  {phase}  ({n} frames, budget util {util:.0f}%)")
            print(f"    mean={mean:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms  max={mx:.1f}ms")

            # histogram
            edges = HISTOGRAM_EDGES_MS
            buckets = [0] * (len(edges))  # last bucket is > final edge
            for v in s:
                placed = False
                for i in range(len(edges) - 1):
                    if v < edges[i + 1]:
                        buckets[i] += 1
                        placed = True
                        break
                if not placed:
                    buckets[-1] += 1

            bar_max = max(buckets) if buckets else 1
            for i, count in enumerate(buckets):
                if i < len(edges) - 1:
                    label = f"{edges[i]:>2}-{edges[i+1]:<2}ms"
                else:
                    label = f">{edges[-1]:<2} ms"
                pct = count / n * 100.0
                bar = "\u2588" * max(1, int(count / bar_max * 30)) if count else ""
                print(f"    {label}: {count:>5} ({pct:4.0f}%)  {bar}")

        over = sum(1 for samples in self._phase_samples.values()
                   for v in samples if v > FRAME_DURATION_MS)
        total = sum(len(s) for s in self._phase_samples.values())
        print(f"\n  Frames over {FRAME_DURATION_MS:.0f}ms budget: "
              f"{over}/{total}" + (f" ({over/total*100:.1f}%)" if total else ""))
        print("=" * 64)

    # -- FrameProcessor interface ---------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            await self.push_frame(frame, direction)
            return

        now = time.perf_counter()

        # inter-frame arrival
        if self._last_arrival > 0:
            interval_ms = (now - self._last_arrival) * 1000.0
            self._window_arrivals.append(interval_ms)
        self._last_arrival = now

        # queue depth sample
        qd = self._queue_depth()
        if qd > self._window_max_qdepth:
            self._window_max_qdepth = qd

        # start window phase tracking on first frame of window
        if not self._window:
            self._window_start_phase = self._state.phase

        # --- the measurement: time the entire downstream chain ---
        t0 = time.perf_counter()
        await self.push_frame(frame, direction)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self._total_frames += 1
        self._window.append(elapsed_ms)
        self._phase_samples[self._state.phase].append(elapsed_ms)

        if len(self._window) >= DUTY_CYCLE_WINDOW:
            self._emit_periodic()


# ---------------------------------------------------------------------------
# RecorderStateStub — downstream port stubbed, upstream port real
# ---------------------------------------------------------------------------

class RecorderStateStub(RecorderState):
    """RecorderState with real state machine but stubbed IPC.

    Signals are collected into self.events for inspection.
    Ring buffer writes go to an InProcessRingBuffer to exercise the real
    per-frame memcpy cost (replaces the pure no-op from the original stub).
    """

    def __init__(self):
        super().__init__(pipe=None, shm=None)
        self.events: list[dict] = []
        self._ring = InProcessRingBuffer()

    def write_audio(self, frame_bytes: bytes) -> None:
        self._ring.write(frame_bytes)
        self._write_pos = self._ring.write_pos

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

    def ring_summary(self) -> str:
        return self._ring.summary()


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
# RingBufferWriter — writes audio frames via state.write_audio()
# ---------------------------------------------------------------------------

class RingBufferWriter(FrameProcessor):
    """Writes audio frames to the ring buffer via state.write_audio().

    In Track 2 this writes to the InProcessRingBuffer inside RecorderStateStub,
    exercising the per-frame memcpy cost of the real SharedMemory write path.
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

async def direct_command_driver(state: RecorderStateStub,
                                duty_probe: DutyCycleProbe | None = None):
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

    print("\n" + "=" * 60)
    print("RING BUFFER SUMMARY")
    print("=" * 60)
    print(state.ring_summary())
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ENABLE_DUTY_CYCLE = os.environ.get("ENABLE_DUTY_CYCLE", "0") == "1"


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
    ring_writer = RingBufferWriter(state=state)

    input_transport = transport.input()

    duty_probe = None
    if ENABLE_DUTY_CYCLE:
        duty_probe = DutyCycleProbe(state=state, transport_input=input_transport)

    # Wire state refs
    state.set_transport(input_transport)
    state.set_vad(vad_processor)
    state.set_oww(wake_processor)
    state.set_ring_writer(ring_writer)

    # Compose pipeline — duty cycle probe is inserted only when enabled
    processors = [input_transport]
    if duty_probe:
        processors.append(duty_probe)
    processors.extend([vad_processor, wake_processor, ring_writer])

    pipeline = Pipeline(processors)
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
    if duty_probe:
        print(f"  Duty cycle probe ENABLED  (window={DUTY_CYCLE_WINDOW} frames, "
              f"budget={FRAME_DURATION_MS:.0f}ms)")
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

    driver = asyncio.create_task(direct_command_driver(state, duty_probe))

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        print("\nPipeline cancelled.")
    finally:
        driver.cancel()
        if duty_probe:
            duty_probe.print_final_summary()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        print("\nEU-3c harness finished.")
        print(f"Events collected: {len(state.events)}")
        for i, ev in enumerate(state.events):
            print(f"  {i}: {ev}")


if __name__ == "__main__":
    asyncio.run(main())
