"""
recorder_child.py — Complete recorder subprocess (EU-3d merge).

Runs in a forked child process pinned to core 0. Owns the microphone via
Pipecat + PyAudio, runs OWW wake word detection and Silero VAD, writes
audio to a SharedMemory ring buffer, and sends events over a Pipe to the
master process.

Combines Track 1's real downstream port (ring buffer + pipe signals) with
Track 2's real Pipecat pipeline (GatedVADProcessor, OpenWakeWordProcessor).

Duty cycle instrumentation (LOG_LEVEL=PERF or TRACE):
  Bookend processors measure end-to-end pipeline traversal time per audio
  frame. Composed into the pipeline only when the root logger level is at or
  below PERF (8). Zero overhead otherwise. Per-phase histogram, arrival
  cadence, and OWW predict summary at exit.

Entry point: recorder_child_entry(pipe, shm_name) — used as
multiprocessing.Process target by the master.
"""

import asyncio
import logging
import math
import os
import signal
import struct
import time

import numpy as np
from collections import defaultdict, deque
from multiprocessing.shared_memory import SharedMemory

from log_config import configure_logging, PERF, TRACE

logger = logging.getLogger("recorder_child")

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

def _duty_cycle_enabled() -> bool:
    """True when the root logger level is at or below PERF (8).

    Checked once during child process setup, after configure_logging() has run.
    Avoids an env-var dependency — LOG_LEVEL is the single verbosity control.
    """
    return logging.getLogger().isEnabledFor(PERF)


# ---------------------------------------------------------------------------
# Duty cycle instrumentation — bookend entry/exit processors + collector
# ---------------------------------------------------------------------------
#
# InputAudioRawFrame inherits from SystemFrame. System frames are awaited
# inline in each processor's input task — they do NOT pass through the
# process queue. When OWW's input task awaits predict(), no other processor
# task can run. The elapsed time from DutyCycleEntry stamp to DutyCycleExit
# collection captures the full blocking cost of all intermediate processors.
# ---------------------------------------------------------------------------

DUTY_CYCLE_WINDOW  = 100
FRAME_DURATION_MS  = 20.0
HISTOGRAM_EDGES_MS = (0, 5, 10, 15, 20)
PREDICT_REPORT_INTERVAL = 25


class QueueDepthMonitor:
    """Always-on tripwire for audio input queue backlog.

    Reads transport_input._audio_in_queue.qsize() once per audio frame.
    Prints an immediate alarm when depth exceeds ALARM_THRESHOLD.
    Designed to be called from exactly one pipeline processor per frame
    (DutyCycleEntry when duty cycle is enabled, GatedVADProcessor otherwise).
    """

    ALARM_THRESHOLD = 2

    def __init__(self, transport_input):
        self._transport_input = transport_input
        self._max_depth_seen: int = 0
        self._alarm_count: int = 0
        self._consecutive_alarms: int = 0

    def check(self) -> int:
        qd = self._read_depth()
        if qd < 0:
            return qd
        if qd > self._max_depth_seen:
            self._max_depth_seen = qd
        if qd > self.ALARM_THRESHOLD:
            self._alarm_count += 1
            self._consecutive_alarms += 1
            logger.warning("[QDEPTH ALARM] depth=%d consecutive=%d total=%d max_seen=%d",
                           qd, self._consecutive_alarms, self._alarm_count, self._max_depth_seen)
        else:
            self._consecutive_alarms = 0
        return qd

    @property
    def max_depth_seen(self) -> int:
        return self._max_depth_seen

    def _read_depth(self) -> int:
        t = self._transport_input
        if t and hasattr(t, '_audio_in_queue'):
            return t._audio_in_queue.qsize()
        return -1


# noinspection DuplicatedCode
class DutyCycleCollector:
    """Aggregates entry/exit timestamps and computes duty cycle statistics."""

    def __init__(self, state: RecorderState, monitor: QueueDepthMonitor, transport_input=None):
        self._state = state
        self._transport_input = transport_input
        if not isinstance(monitor, QueueDepthMonitor):
            raise RuntimeError("expected monitor")
        self._monitor = monitor

        self._entry_stamps: deque[float] = deque()

        self._window: list[float] = []
        self._window_start_phase: str = "dormant"
        self._window_arrivals: list[float] = []
        self._window_max_qdepth: int = 0

        self._phase_samples: dict[str, list[float]] = defaultdict(list)
        self._phase_arrivals: dict[str, list[float]] = defaultdict(list)

        self._last_arrival: float = 0.0
        self._total_frames: int = 0

    def stamp_entry(self) -> None:
        now = time.perf_counter()
        self._entry_stamps.append(now)

        if self._last_arrival > 0:
            gap_ms = (now - self._last_arrival) * 1000.0
            self._window_arrivals.append(gap_ms)
            self._phase_arrivals[self._state.phase].append(gap_ms)
        self._last_arrival = now

        qd = self._monitor.check()
        if qd > self._window_max_qdepth:
            self._window_max_qdepth = qd

    def stamp_exit(self) -> None:
        if not self._entry_stamps:
            return
        t0 = self._entry_stamps.popleft()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self._total_frames += 1

        if not self._window:
            self._window_start_phase = self._state.phase

        self._window.append(elapsed_ms)
        self._phase_samples[self._state.phase].append(elapsed_ms)

        if len(self._window) >= DUTY_CYCLE_WINDOW:
            self._emit_periodic()

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
            a_mean = sum(self._window_arrivals) / len(self._window_arrivals)
            a_var = sum((x - a_mean) ** 2 for x in self._window_arrivals) / len(self._window_arrivals)
            a_std = a_var ** 0.5
            arrival_str = f"  arrival: mean={a_mean:.1f}ms σ={a_std:.1f}ms"

        qdepth_str = ""
        if self._window_max_qdepth >= 0:
            qdepth_str = f"  q_max={self._window_max_qdepth}"

        logger.log(PERF, "[DUTY/%d] %s: mean=%.1fms p95=%.1fms max=%.1fms util=%.0f%%%s%s",
                   self._total_frames, phase_lbl, mean, p95, mx, util,
                   arrival_str, qdepth_str)

        self._window.clear()
        self._window_arrivals.clear()
        self._window_max_qdepth = 0
        self._window_start_phase = self._state.phase

    def print_final_summary(self, wake_processor=None) -> None:
        lines = ["\n" + "=" * 64, "DUTY CYCLE SUMMARY", "=" * 64]

        for phase in ("wake_listen", "capture", "idle", "dormant"):
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

            lines.append(f"\n  {phase}  ({n} frames, budget util {util:.0f}%)")
            lines.append(f"    mean={mean:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms  max={mx:.1f}ms")

            edges = HISTOGRAM_EDGES_MS
            buckets = [0] * (len(edges))
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
                lines.append(f"    {label}: {count:>5} ({pct:4.0f}%)  {bar}")

        over = sum(1 for samples in self._phase_samples.values()
                   for v in samples if v > FRAME_DURATION_MS)
        total = sum(len(s) for s in self._phase_samples.values())
        lines.append(f"\n  Frames over {FRAME_DURATION_MS:.0f}ms budget: "
                     f"{over}/{total}" + (f" ({over/total*100:.1f}%)" if total else ""))

        if self._phase_arrivals:
            lines.append("\n  Arrival cadence (inter-frame intervals):")
            for phase in ("wake_listen", "capture", "idle", "dormant"):
                arrivals = self._phase_arrivals.get(phase)
                if not arrivals:
                    continue
                sa = sorted(arrivals)
                n = len(sa)
                a_mean = sum(sa) / n
                a_var = sum((x - a_mean) ** 2 for x in sa) / n
                a_std = a_var ** 0.5
                lines.append(f"    {phase} ({n}): mean={a_mean:.1f}ms σ={a_std:.1f}ms "
                              f"min={sa[0]:.1f}ms max={sa[-1]:.1f}ms")

        if wake_processor:
            lines.append("")
            lines.append(wake_processor.predict_summary())

        lines.append("=" * 64)
        logger.info("\n".join(lines))


# noinspection DuplicatedCode
class DutyCycleEntry(FrameProcessor):
    """Pipeline head bookend: stamps each audio frame's arrival time."""

    def __init__(self, collector: DutyCycleCollector):
        super().__init__()
        self._collector = collector
        self._first_frame_logged = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            if not self._first_frame_logged:
                self._log_first_frame(frame)
                self._first_frame_logged = True
            self._collector.stamp_entry()
        await self.push_frame(frame, direction)

    @staticmethod
    def _log_first_frame(frame):
        audio_bytes = len(frame.audio)
        samples = audio_bytes // 2
        sr = getattr(frame, 'sample_rate', '?')
        ch = getattr(frame, 'num_channels', '?')
        dur = f"{samples / sr * 1000:.1f}ms" if isinstance(sr, (int, float)) and sr > 0 else "?"
        logger.debug("[DUTY] first audio frame: %d bytes, %d samples, sr=%s Hz, ch=%s, duration=%s",
                     audio_bytes, samples, sr, ch, dur)


class DutyCycleExit(FrameProcessor):
    """Pipeline tail bookend: completes the timing measurement."""

    def __init__(self, collector: DutyCycleCollector):
        super().__init__()
        self._collector = collector

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
            self._collector.stamp_exit()
        await self.push_frame(frame, direction)


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

    def __init__(self, *, vad_analyzer, state: RecorderState,
                 monitor: QueueDepthMonitor | None = None, **kwargs):
        super().__init__(**kwargs)
        self.state = state
        self._monitor = monitor
        self._vad_analyzer = vad_analyzer
        self._vad_controller = VADController(vad_analyzer)

        @self._vad_controller.event_handler("on_speech_started")
        async def on_speech_started(_controller):
            logger.info("[VAD] speech_started (after %d frames)", self.state.vad_frame_count)
            self.state.signal_vad_started()

        @self._vad_controller.event_handler("on_speech_stopped")
        async def on_speech_stopped(_controller):
            logger.info("[VAD] speech_stopped (after %d frames)", self.state.vad_frame_count)
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
            if self._monitor:
                self._monitor.check()
            self.state.inc_total_frames()
            if self.state.capture:
                self.state.inc_vad_frames()
                await self._vad_controller.process_frame(frame)


# ---------------------------------------------------------------------------
# OpenWakeWordProcessor — OWW inference gated to WAKE_LISTEN phase only
# ---------------------------------------------------------------------------

# noinspection DuplicatedCode
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
        logger.info("loading openwakeword models...")
        self.model = OWWModel()
        self._chunks = []
        self.last_detection_time = 0.0
        self.DEBOUNCE_SECONDS = 1.8
        self._predict_times: deque[float] = deque(maxlen=500)
        self._predict_count: int = 0
        self._frames_in_wake: int = 0
        self._window_predict_times: list[float] = []
        self._pending_predict: asyncio.Task | None = None
        logger.info("openwakeword ready")

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
            elapsed_ms = (time.perf_counter() - t_pred) * 1000.0
            self._predict_times.append(elapsed_ms)
            self._predict_count += 1
            self._window_predict_times.append(elapsed_ms)
            if len(self._window_predict_times) >= PREDICT_REPORT_INTERVAL:
                self._emit_predict_window()
            if not self.state.wake_listen:
                return
            current_time = time.time()
            for wakeword, score in predictions.items():
                if (wakeword == "hey_jarvis"
                        and score > 0.5
                        and (current_time - self.last_detection_time)
                            > self.DEBOUNCE_SECONDS):
                    logger.info("WAKE DETECTED -- '%s'  |  score: %.3f", wakeword, score)
                    self.last_detection_time = current_time
                    self.state.signal_wake_detected(score, wakeword)

    def _emit_predict_window(self) -> None:
        s = sorted(self._window_predict_times)
        n = len(s)
        mean = sum(s) / n
        p95 = DutyCycleCollector._percentile(s, 0.95)
        mx = s[-1]
        logger.log(PERF, "[OWW/%d] predict: n=%d mean=%.1fms p95=%.1fms max=%.1fms",
                   self._predict_count, n, mean, p95, mx)
        self._window_predict_times.clear()

    def predict_summary(self) -> str:
        if not self._predict_count:
            return "  No OWW predict calls recorded."
        s = sorted(self._predict_times)
        n = len(s)
        mean = sum(s) / n
        p95 = DutyCycleCollector._percentile(s, 0.95)
        p99 = DutyCycleCollector._percentile(s, 0.99)
        mx = s[-1]
        ratio = self._frames_in_wake / self._predict_count
        window = f" (last {n})" if n < self._predict_count else ""
        lines = [
            f"  OWW predict: {self._predict_count} calls in "
            f"{self._frames_in_wake} wake frames "
            f"(1 per {ratio:.1f} frames)",
            f"    mean={mean:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms  "
            f"max={mx:.1f}ms{window}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# AudioFrameWriter — writes audio frames via state.write_audio()
# ---------------------------------------------------------------------------

class AudioFrameWriter(FrameProcessor):
    """Pipeline tail: writes every audio frame to the ring buffer.

    Delegates to state.write_audio(), which performs the SharedMemory
    memcpy and advances write_pos. Skips writes in DORMANT phase.

    Instrumentation: logs on the first frame of each phase, then every
    _LOG_INTERVAL frames, printing write_pos and 4 raw PCM samples so
    zeros (silent/missing audio) are immediately visible in output.
    Also logs a phase-boundary summary when the phase transitions.
    """

    _LOG_INTERVAL = 50   # ~1 s at 20 ms/frame

    def __init__(self, state: RecorderState):
        super().__init__()
        self.state = state
        self._frames_written: int = 0   # total across all phases
        self._phase_frames: int = 0     # resets on each phase change
        self._last_phase: str = ""

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame) and not self.state.dormant:
            phase = self.state.phase

            if phase != self._last_phase:
                if self._last_phase:
                    logger.debug("[ring/write] phase %s→%s: wrote %d frames, write_pos=%d",
                                 self._last_phase, phase, self._phase_frames, self.state.write_pos)
                self._phase_frames = 0
                self._last_phase = phase

            self.state.write_audio(frame.audio)
            self._frames_written += 1
            self._phase_frames += 1

            if self._phase_frames == 1 or self._phase_frames % self._LOG_INTERVAL == 0:
                wp = self.state.write_pos
                head = struct.unpack_from('<4h', frame.audio)
                logger.log(TRACE, "[ring/write] phase=%s frame=%d write_pos=%d sample[0:4]=%s",
                           phase, self._phase_frames, wp, head)

        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# command_listener — routes pipe commands to state.set_phase()
# ---------------------------------------------------------------------------

async def command_listener(state: RecorderChild, pipe, initiate_shutdown) -> None:
    """Poll pipe for master commands; call set_phase() on each.

    Returns on SHUTDOWN (after delegating to initiate_shutdown).
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
            elif cmd == "SET_IDLE":
                await state.set_phase("idle")
            elif cmd == "SHUTDOWN":
                logger.info("[child] SHUTDOWN command received")
                await initiate_shutdown()
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

        input_transport = transport.input()

        qdepth_monitor = QueueDepthMonitor(transport_input=input_transport)

        duty_cycle_on = _duty_cycle_enabled()

        vad_processor = GatedVADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(stop_secs=1.8, start_secs=0.2),
            ),
            state=state,
            monitor=qdepth_monitor if not duty_cycle_on else None,
        )

        wake_processor = OpenWakeWordProcessor(state=state)
        audio_writer = AudioFrameWriter(state=state)

        duty_collector = None
        if duty_cycle_on:
            duty_collector = DutyCycleCollector(
                state=state, monitor=qdepth_monitor, transport_input=input_transport,
            )

        state.set_transport(input_transport)
        state.set_vad(vad_processor)
        state.set_oww(wake_processor)
        state.set_ring_writer(audio_writer)

        processors = [input_transport]
        if duty_collector:
            processors.append(DutyCycleEntry(duty_collector))
        processors.extend([vad_processor, wake_processor, audio_writer])
        if duty_collector:
            processors.append(DutyCycleExit(duty_collector))

        pipeline = Pipeline(processors)
        runner = PipelineRunner()
        task = PipelineTask(pipeline)

        pipe.send({"cmd": "READY"})
        logger.info("[child] queue depth monitor ENABLED (alarm threshold=%d)",
                    QueueDepthMonitor.ALARM_THRESHOLD)
        if duty_collector:
            logger.info("[child] duty cycle probe ENABLED (window=%d frames, budget=%.0fms)",
                        DUTY_CYCLE_WINDOW, FRAME_DURATION_MS)

        loop = asyncio.get_running_loop()
        shutdown_initiated = False

        async def _initiate_shutdown():
            """Unified shutdown path for both SIGINT and SHUTDOWN command.

            Once-only guard: first caller wins, subsequent calls are no-ops.
            Sends SHUTDOWN_COMMENCED, does the safe teardown sequence
            (stop stream via set_phase dormant, then cancel pipeline),
            then returns. SHUTDOWN_FINISHED is sent from the finally block
            after all cleanup completes.
            """
            nonlocal shutdown_initiated
            if shutdown_initiated:
                return
            shutdown_initiated = True
            try:
                pipe.send({"cmd": "SHUTDOWN_COMMENCED"})
            except Exception:
                pass
            logger.info("[child] shutdown commenced — draining pipeline...")
            await state.set_phase("dormant")
            await task.cancel()

        def _on_signal(name):
            if not shutdown_initiated:
                logger.info("[child] %s — initiating safe shutdown...", name)
                asyncio.create_task(_initiate_shutdown())

        loop.add_signal_handler(signal.SIGINT, lambda: _on_signal("SIGINT"))
        loop.add_signal_handler(signal.SIGTERM, lambda: _on_signal("SIGTERM"))

        listener = asyncio.create_task(
            command_listener(state, pipe, _initiate_shutdown)
        )

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
            logger.info("[QDEPTH] max_depth_seen=%d total_alarms=%d",
                        qdepth_monitor.max_depth_seen, qdepth_monitor._alarm_count)
            if duty_collector:
                duty_collector.print_final_summary(wake_processor)
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    finally:
        try:
            pipe.send({"cmd": "SHUTDOWN_FINISHED"})
            logger.info("[child] SHUTDOWN_FINISHED sent")
        except Exception:
            pass
        shm.close()
        logger.info("[child] exiting")


# ---------------------------------------------------------------------------
# Process entry point (multiprocessing.Process target)
# ---------------------------------------------------------------------------

def recorder_child_entry(pipe, shm_name: str) -> None:
    """Pin to core 0, elevate scheduling priority, then run the async main loop.

    Scheduling: SCHED_FIFO (real-time) if permitted, otherwise nice -10.
    Ensures the audio pipeline preempts normal processes on core 0.

    SIGINT is deferred (SIG_IGN) until the asyncio event loop installs its
    own handler via add_signal_handler. This prevents a narrow race where
    ^C arrives between process start and handler registration.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging()
    os.sched_setaffinity(0, {0})
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
        logger.info("[child] SCHED_FIFO priority 50")
    except PermissionError:
        try:
            os.nice(-10)
            logger.info("[child] nice -10 (SCHED_FIFO unavailable)")
        except PermissionError:
            logger.warning("[child] could not elevate priority")
    asyncio.run(recorder_child_main(pipe, shm_name))
