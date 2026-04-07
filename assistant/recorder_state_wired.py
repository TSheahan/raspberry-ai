"""
recorder_state_wired.py — WiredRecorderState: contract core + pipe, ring, Pipecat workers.

Mirrors the MasterState / WiredMasterState split: `RecorderState` in recorder_state.py
holds phase/counters and transition gating; this module performs stream, OWW, Silero,
ring writes, and pipe signals.

TODO(FUTURE): constructor injection could replace post-construction set_* (all workers
exist before state in recorder_child_main).
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any

import numpy as np
from loguru import logger

from logging_setup import TRACE
from audio_shm_ring import AudioShmRingWriter
from phase_protocol import TransitionKind
from recorder_state import RecorderState


class WiredRecorderState(RecorderState):
    """RecorderState with SharedMemory ring writes, pipe IPC, and worker weakrefs."""

    def __init__(self) -> None:
        super().__init__()
        self._pipe: Any = None
        self._shm_ring_writer: AudioShmRingWriter | None = None
        self._vad_ref: Any = None
        self._oww_ref: Any = None
        self._transport_ref: Any = None
        self._ring_writer_ref: Any = None

    def set_pipe(self, pipe: Any) -> None:
        self._pipe = pipe

    def set_shm_ring_writer(self, ring_writer: AudioShmRingWriter) -> None:
        self._shm_ring_writer = ring_writer

    def set_vad(self, vad_processor: Any) -> None:
        self._vad_ref = weakref.ref(vad_processor)

    def set_oww(self, oww_processor: Any) -> None:
        self._oww_ref = weakref.ref(oww_processor)

    def set_transport(self, transport: Any) -> None:
        self._transport_ref = transport

    def set_ring_writer(self, ring_writer: Any) -> None:
        self._ring_writer_ref = weakref.ref(ring_writer)

    # -----------------------------------------------------------------------
    # Phase transitions (worker orchestration, then commit_phase)
    # -----------------------------------------------------------------------

    async def set_phase(self, new_phase: str) -> None:
        """Transition to new_phase; _phase updates only after worker side-effects.

        Processors reading ``phase`` during an effect still see the old phase.
        """
        snap = self.gate_phase_transition(new_phase)
        if snap is None:
            logger.error("[state] rejected unknown phase: {!r}", new_phase)
            return
        if snap.kind == TransitionKind.STALE:
            logger.warning(
                "[state] rejected illegal transition {} → {}",
                snap.old_phase,
                new_phase,
            )
            return
        if snap.kind == TransitionKind.NOOP:
            self.signal_state_changed()
            return

        old_phase = snap.old_phase

        if new_phase == "dormant":
            await self._stop_stream()

        if old_phase == "wake_listen":
            await self._drain_oww_predict()
            self._clear_oww()

        if new_phase == "wake_listen":
            self._reset_oww_full()
        elif new_phase == "capture":
            await self._reset_silero()

        self.apply_entry_vad_frame_reset(new_phase)

        if old_phase == "dormant":
            await self._start_stream()

        self.commit_phase(new_phase)
        self.signal_state_changed()

    # -----------------------------------------------------------------------
    # Downstream port — ring + pipe
    # -----------------------------------------------------------------------

    def write_audio(self, frame_bytes: bytes) -> None:
        assert self._shm_ring_writer is not None
        self._shm_ring_writer.write(frame_bytes)
        self.update_write_pos(self._shm_ring_writer.write_pos)

    def signal_state_changed(self) -> None:
        assert self._pipe is not None
        self._pipe.send({"cmd": "STATE_CHANGED", "state": self.phase})

    def signal_wake_detected(self, score: float, keyword: str) -> None:
        assert self._pipe is not None
        self._pipe.send({
            "cmd": "WAKE_DETECTED",
            "write_pos": self.write_pos,
            "score": score,
            "keyword": keyword,
        })

    def signal_vad_started(self) -> None:
        assert self._pipe is not None
        self._pipe.send({"cmd": "VAD_STARTED", "write_pos": self.write_pos})

    def signal_vad_stopped(self) -> None:
        assert self._pipe is not None
        self._pipe.send({"cmd": "VAD_STOPPED", "write_pos": self.write_pos})

    # -----------------------------------------------------------------------
    # Stream lifecycle
    # -----------------------------------------------------------------------

    async def _start_stream(self) -> None:
        t = self._transport_ref
        for _ in range(20):
            if t and hasattr(t, "_in_stream") and t._in_stream:
                t._in_stream.start_stream()
                logger.debug("[state] stream started")
                return
            await asyncio.sleep(0.010)
        logger.warning("[stream] start skipped — _in_stream not ready after 200ms")

    async def _stop_stream(self) -> None:
        t = self._transport_ref
        if t and hasattr(t, "_in_stream") and t._in_stream:
            t._in_stream.stop_stream()
            await asyncio.sleep(0.1)
            logger.debug("[state] stream stopped")
        else:
            logger.warning("[stream] stop skipped — no truthy _in_stream")

    # -----------------------------------------------------------------------
    # OWW / Silero
    # -----------------------------------------------------------------------

    async def _drain_oww_predict(self) -> None:
        oww = self._oww_ref() if self._oww_ref else None
        if oww is None:
            return
        pending = getattr(oww, "_pending_predict", None)
        if pending and not pending.done():
            logger.debug("[state] draining pending OWW predict...")
            await pending
            logger.debug("[state] OWW predict drained")

    def _reset_oww_full(self) -> None:
        oww = self._oww_ref() if self._oww_ref else None
        if oww is None:
            return
        oww.model.reset()
        pp = oww.model.preprocessor
        if hasattr(pp, "raw_data_buffer"):
            pp.raw_data_buffer.clear()
        if hasattr(pp, "melspectrogram_buffer"):
            pp.melspectrogram_buffer = np.zeros(
                pp.melspectrogram_buffer.shape, dtype=pp.melspectrogram_buffer.dtype
            )
        if hasattr(pp, "feature_buffer"):
            pp.feature_buffer = np.zeros(
                pp.feature_buffer.shape, dtype=pp.feature_buffer.dtype
            )
        if hasattr(pp, "accumulated_samples"):
            pp.accumulated_samples = 0
        oww._chunks = []
        oww.last_detection_time = time.time()
        logger.debug("[state] OWW full reset")

    def _clear_oww(self) -> None:
        oww = self._oww_ref() if self._oww_ref else None
        if oww is None:
            return
        oww._chunks = []
        logger.log(TRACE, "[state] OWW chunks cleared")

    async def _reset_silero(self) -> None:
        vad = self._vad_ref() if self._vad_ref else None
        if vad is None:
            return
        if hasattr(vad, "_vad_analyzer") and hasattr(vad._vad_analyzer, "_model"):
            model = vad._vad_analyzer._model
            if hasattr(model, "reset_states"):
                model.reset_states()
                logger.debug("[state] Silero LSTM reset")
                return
        logger.warning("[state] could not find Silero _model.reset_states()")
