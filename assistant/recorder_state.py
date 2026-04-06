"""
recorder_state.py — RecorderState base class (EU-3a skeleton).

RecorderState is the seam between the Pipecat pipeline (upstream port)
and the process boundary (downstream port).

Upstream port — called by processors inside the recorder child:
  state.phase / .dormant / .wake_listen / .capture   (phase reads)
  state.write_audio(frame_bytes)                      (ring buffer write)
  state.signal_wake_detected(score, keyword)
  state.signal_vad_started()
  state.signal_vad_stopped()
  state.inc_vad_frames() / inc_total_frames()

Downstream port — crosses the process boundary:
  write_audio()        → SharedMemory ring buffer (subclass provides)
  signal_*()           → pipe.send(dict)           (subclass provides)
  _start_stream()      → PyAudio start             (subclass provides)
  _stop_stream()       → PyAudio stop              (subclass provides)
  _reset_oww_full()    → OWW 5-buffer reset        (EU-3c fills in on base)
  _clear_oww()         → OWW chunk clear           (EU-3c fills in on base)
  _reset_silero()      → Silero LSTM reset         (EU-3c fills in on base)

Subclass must provide: signal_*(), write_audio() (raise NotImplementedError here).
Pipecat-coupled methods (_reset_oww_full, _clear_oww, _reset_silero,
_start_stream, _stop_stream) are implemented on this class using weakref
wiring so that RecorderStateStub inherits them without overriding.
"""

import asyncio
import time
import weakref

import numpy as np

from logging_setup import TRACE
from loguru import logger


class RecorderState:
    """Central state object for the recorder child process.

    Owns the phase transition logic. Processors hold only a reference to this
    object — never to each other. Weak-ref pointers into controlled processors
    prevent reference cycles.
    """

    def __init__(self, pipe, shm):
        """
        Args:
            pipe: multiprocessing.Connection to master, or None (stub-safe).
            shm:  multiprocessing.shared_memory.SharedMemory, or None.
        """
        self._phase             = "dormant"   # "dormant" | "wake_listen" | "capture" | "idle"
        self._write_pos         = 0           # monotonic byte offset; updated by write_audio()
        self._vad_frame_count   = 0           # frames fed to Silero this capture session
        self._total_frame_count = 0           # all audio frames since process start

        self._pipe = pipe   # strong ref (long-lived)
        self._shm  = shm    # strong ref (long-lived)

        self._vad_ref         = None   # weakref → GatedVADProcessor
        self._oww_ref         = None   # weakref → OpenWakeWordProcessor
        self._transport_ref   = None   # strong ref → input transport (long-lived, no cycle)
        self._ring_writer_ref = None   # weakref → AudioShmRingWriteProcessor

    # -----------------------------------------------------------------------
    # Wiring  (called once in child main, after all objects are constructed)
    # -----------------------------------------------------------------------

    def set_vad(self, vad_processor) -> None:
        self._vad_ref = weakref.ref(vad_processor)

    def set_oww(self, oww_processor) -> None:
        self._oww_ref = weakref.ref(oww_processor)

    def set_transport(self, transport) -> None:
        self._transport_ref = transport          # strong ref intentional

    def set_ring_writer(self, ring_writer) -> None:
        self._ring_writer_ref = weakref.ref(ring_writer)

    # -----------------------------------------------------------------------
    # Read-only properties
    # -----------------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def dormant(self) -> bool:
        return self._phase == "dormant"

    @property
    def wake_listen(self) -> bool:
        return self._phase == "wake_listen"

    @property
    def capture(self) -> bool:
        return self._phase == "capture"

    @property
    def idle(self) -> bool:
        return self._phase == "idle"

    @property
    def write_pos(self) -> int:
        return self._write_pos

    @property
    def vad_frame_count(self) -> int:
        return self._vad_frame_count

    @property
    def total_frame_count(self) -> int:
        return self._total_frame_count

    # -----------------------------------------------------------------------
    # Phase transitions  (fully implemented — downstream methods are abstract)
    # -----------------------------------------------------------------------

    async def set_phase(self, new_phase: str) -> None:
        """Transition to new_phase, firing ordered side-effects.

        _phase is set AFTER all side-effects complete so that processors
        reading state.phase inside a side-effect still see the old phase.
        This prevents re-entrant gating issues.
        """
        old_phase = self._phase

        if old_phase == new_phase:
            self.signal_state_changed()
            return

        # --- Leading entry side-effects ---
        if new_phase == "dormant":
            await self._stop_stream()          # stream down first

        # --- Exit side-effects ---
        if old_phase == "wake_listen":
            await self._drain_oww_predict()    # finish async predict before Silero starts
            self._clear_oww()                  # clear pending OWW chunks
        elif old_phase == "capture":
            pass                               # no cleanup on capture exit

        # --- Entry side-effects ---
        if new_phase == "wake_listen":
            self._reset_oww_full()             # 5-buffer reset; prevents false positives
            self._vad_frame_count = 0
        elif new_phase == "capture":
            await self._reset_silero()         # LSTM reset before first frame
            self._vad_frame_count = 0

        # --- Trailing exit side-effects ---
        if old_phase == "dormant":
            await self._start_stream()         # stream up last

        self._phase = new_phase
        self.signal_state_changed()

    # -----------------------------------------------------------------------
    # Signal emission  (upstream port → downstream port; subclass provides)
    # -----------------------------------------------------------------------

    def signal_state_changed(self) -> None:
        """Called by set_phase after every transition. Public so stubs can override."""
        raise NotImplementedError

    def signal_wake_detected(self, score: float, keyword: str) -> None:
        """Called by OpenWakeWordProcessor on detection."""
        raise NotImplementedError

    def signal_vad_started(self) -> None:
        """Called by GatedVADProcessor on speech onset."""
        raise NotImplementedError

    def signal_vad_stopped(self) -> None:
        """Called by GatedVADProcessor on speech offset."""
        raise NotImplementedError

    # -----------------------------------------------------------------------
    # Audio write  (subclass provides; must update self._write_pos)
    # -----------------------------------------------------------------------

    def write_audio(self, frame_bytes: bytes) -> None:
        """Write frame_bytes to the ring buffer and advance self._write_pos.

        Called by AudioShmRingWriteProcessor.process_frame on every AudioRawFrame when
        not dormant. Must complete in under 1ms (runs on asyncio event loop).
        """
        raise NotImplementedError

    # -----------------------------------------------------------------------
    # Stream lifecycle  (real impl using _transport_ref)
    # -----------------------------------------------------------------------

    async def _start_stream(self) -> None:
        """Start the PyAudio input stream.

        Retries for up to 200ms to tolerate the startup race: READY is sent
        from recorder_process before runner.run() is called, so the first
        SET_WAKE_LISTEN can arrive before _in_stream exists. In practice the
        stream is ready within a few ms of the first audio frame.
        """
        t = self._transport_ref
        for _ in range(20):  # 20 × 10ms = 200ms ceiling
            if t and hasattr(t, '_in_stream') and t._in_stream:
                t._in_stream.start_stream()
                logger.debug("[state] stream started")
                return
            await asyncio.sleep(0.010)
        logger.warning("[stream] start skipped — _in_stream not ready after 200ms")

    async def _stop_stream(self) -> None:
        """Stop the PyAudio input stream.

        Sleeps briefly after stopping to let the driver's I2C teardown
        path drain before any subsequent close/cleanup.
        """
        t = self._transport_ref
        if t and hasattr(t, '_in_stream') and t._in_stream:
            t._in_stream.stop_stream()
            await asyncio.sleep(0.1)
            logger.debug("[state] stream stopped")
        else:
            logger.warning("[stream] stop skipped — no truthy _in_stream")

    # -----------------------------------------------------------------------
    # OWW / Silero ops  (EU-3c fills real impls on base using _oww_ref / _vad_ref)
    # -----------------------------------------------------------------------

    async def _drain_oww_predict(self) -> None:
        """Await any pending async OWW predict task.

        Prevents concurrent ONNX sessions (OWW + Silero) on wake_listen→capture.
        Must complete before _reset_silero runs.
        """
        oww = self._oww_ref() if self._oww_ref else None
        if oww is None:
            return
        pending = getattr(oww, '_pending_predict', None)
        if pending and not pending.done():
            logger.debug("[state] draining pending OWW predict...")
            await pending
            logger.debug("[state] OWW predict drained")

    def _reset_oww_full(self) -> None:
        """Full 5-buffer OWW reset: prediction_buffer, raw_data_buffer,
        melspectrogram_buffer, feature_buffer, accumulated_samples.
        Must run before the first frame reaches OWW predict after an ungate.
        """
        oww = self._oww_ref() if self._oww_ref else None
        if oww is None:
            return
        oww.model.reset()
        pp = oww.model.preprocessor
        if hasattr(pp, 'raw_data_buffer'):
            pp.raw_data_buffer.clear()
        if hasattr(pp, 'melspectrogram_buffer'):
            pp.melspectrogram_buffer = np.zeros(
                pp.melspectrogram_buffer.shape, dtype=pp.melspectrogram_buffer.dtype)
        if hasattr(pp, 'feature_buffer'):
            pp.feature_buffer = np.zeros(
                pp.feature_buffer.shape, dtype=pp.feature_buffer.dtype)
        if hasattr(pp, 'accumulated_samples'):
            pp.accumulated_samples = 0
        oww._chunks = []
        oww.last_detection_time = time.time()
        logger.debug("[state] OWW full reset")

    def _clear_oww(self) -> None:
        """Clear OWW pending chunk accumulator (lighter than full reset).
        Called on WAKE_LISTEN exit to discard partially-accumulated frames.
        """
        oww = self._oww_ref() if self._oww_ref else None
        if oww is None:
            return
        oww._chunks = []
        logger.log(TRACE, "[state] OWW chunks cleared")

    async def _reset_silero(self) -> None:
        """Reset Silero LSTM hidden states.
        Must complete before the first audio frame reaches the VAD controller.
        """
        vad = self._vad_ref() if self._vad_ref else None
        if vad is None:
            return
        if hasattr(vad, '_vad_analyzer') and hasattr(vad._vad_analyzer, '_model'):
            model = vad._vad_analyzer._model
            if hasattr(model, 'reset_states'):
                model.reset_states()
                logger.debug("[state] Silero LSTM reset")
                return
        logger.warning("[state] could not find Silero _model.reset_states()")

    # -----------------------------------------------------------------------
    # Frame counters  (concrete; called by processors per frame)
    # -----------------------------------------------------------------------

    def inc_vad_frames(self) -> None:
        """Increment count of frames fed to Silero this capture session."""
        self._vad_frame_count += 1

    def inc_total_frames(self) -> None:
        """Increment total audio frame count since process start."""
        self._total_frame_count += 1
