"""
master_state.py — Master process phase belief + VAD gating (master_state_spec.md §4).

Owns believed child phase (from STATE_CHANGED), processing / wake_pos / capture
session refs, and vad_speaking. Does not send on the pipe; the event loop acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from phase_protocol import (
    TransitionKind,
    classify_transition,
    exit_phases_for_belief_update,
    validate_phase,
)

_log = logger.bind(name="master_state")


@dataclass
class StateChangeResult:
    stale: bool = False
    noop: bool = False
    accepted: bool = False


class MasterState:
    """Believed recorder phase and master-local resources (STT session, VAD flag)."""

    def __init__(self) -> None:
        self._phase = "dormant"
        self.processing = False
        self.wake_pos = 0
        self.capture: Any = None
        self.vad_speaking = False

    @property
    def phase(self) -> str:
        return self._phase

    def _vad_context_ok(self) -> bool:
        """Silero events apply while we are in capture or awaiting STATE_CHANGED(capture)."""
        if self._phase == "capture":
            return True
        return self._phase == "wake_listen" and self.capture is not None

    def on_state_changed(self, new_phase: str) -> StateChangeResult:
        if not validate_phase(new_phase):
            _log.error("[master_state] unknown STATE_CHANGED phase: {!r}", new_phase)
            return StateChangeResult(stale=True)

        old = self._phase
        tc = classify_transition(old, new_phase)
        if tc.kind == TransitionKind.STALE:
            _log.debug(
                "[master_state] stale STATE_CHANGED {!r} (belief {!r})",
                new_phase,
                old,
            )
            return StateChangeResult(stale=True)
        if tc.kind == TransitionKind.NOOP:
            _log.debug("[master_state] noop STATE_CHANGED {!r}", new_phase)
            return StateChangeResult(noop=True)

        exit_phases = exit_phases_for_belief_update(old, new_phase)
        for ph in exit_phases:
            self._run_exit_hook(ph)
        self._phase = new_phase
        self._run_entry_hook(new_phase)

        if exit_phases:
            _log.info(
                "[master] state {} → {} (exit hooks: {})",
                old,
                new_phase,
                exit_phases,
            )
        else:
            _log.info("[master] state {} → {}", old, new_phase)
        return StateChangeResult(accepted=True)

    def _run_exit_hook(self, ph: str) -> None:
        if ph == "capture":
            self._teardown_capture_if_live()

    def _run_entry_hook(self, ph: str) -> None:
        if ph == "wake_listen":
            self.vad_speaking = False
            self._teardown_capture_if_live()
            self.processing = False
        elif ph == "capture":
            self.vad_speaking = False

    def _teardown_capture_if_live(self) -> None:
        cap = self.capture
        if cap is None:
            return
        cap.stop_event.set()
        if cap.thread is not None:
            cap.thread.join(timeout=5)
        self.capture = None

    def on_wake_detected(self, write_pos: int, score: float, keyword: str) -> bool:
        if self.processing:
            return False
        if self._phase != "wake_listen":
            _log.debug(
                "[master_state] WAKE_DETECTED ignored (belief {!r})",
                self._phase,
            )
            return False
        self.wake_pos = write_pos
        return True

    def on_vad_started(self, write_pos: int) -> bool:
        if not self._vad_context_ok():
            return False
        if self.vad_speaking:
            return False
        self.vad_speaking = True
        return True

    def on_vad_stopped(self, write_pos: int) -> bool:
        if not self._vad_context_ok():
            return False
        if not self.vad_speaking:
            return False
        self.vad_speaking = False
        return True
