"""
master_state.py — Master process phase belief + VAD gating (master_state_spec.md §4).

Owns believed child phase (from STATE_CHANGED), processing / wake_pos / capture
session refs, vad_speaking, and per-cycle agent `prepare()` tracking (§4f).
Does not send on the pipe; the event loop acts.
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
        # True after SET_CAPTURE until Deepgram thread is started on STATE_CHANGED(capture).
        self.stt_start_pending = False
        # True after agent.prepare() this wake cycle; reset on wake_listen/idle/dormant entry.
        self.agent_prepare_done = False

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def stt_arm_ready(self) -> bool:
        """True when STT thread should be started: capture believed, no session yet, pending flag set."""
        return self._phase == "capture" and self.capture is None and self.stt_start_pending

    @property
    def capture_phase_without_pending_stt(self) -> bool:
        """Capture believed with no session and no pending flag — indicates protocol skew."""
        return self._phase == "capture" and self.capture is None and not self.stt_start_pending

    def _vad_context_ok(self) -> bool:
        """Silero events apply only once we believe capture *and* STT session exists (§2d)."""
        return self._phase == "capture" and self.capture is not None

    def mark_stt_pending_after_set_capture(self) -> None:
        """Call after sending SET_CAPTURE; cleared when STT thread arms or phase abandons capture."""
        self.stt_start_pending = True

    def note_agent_prepare(self) -> None:
        """Call immediately after agent.prepare() on WAKE_DETECTED. Warns if already done this cycle."""
        if self.agent_prepare_done:
            _log.warning(
                "[master_state] note_agent_prepare called again before cycle reset — "
                "possible double wake path",
            )
            return
        self.agent_prepare_done = True

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
            self.teardown_capture()

    def _run_entry_hook(self, ph: str) -> None:
        if ph == "wake_listen":
            self.vad_speaking = False
            self.stt_start_pending = False
            self.agent_prepare_done = False
            self.teardown_capture()
            self.processing = False
        elif ph == "capture":
            self.vad_speaking = False
        elif ph == "idle":
            self.stt_start_pending = False
            self.agent_prepare_done = False
        elif ph == "dormant":
            self.stt_start_pending = False
            self.agent_prepare_done = False

    def teardown_capture(self) -> None:
        """Stop and discard any live capture session."""
        cap = self.capture
        if cap is None:
            return
        cap.stop_event.set()
        if cap.thread is not None:
            cap.thread.join(timeout=5)
        self.capture = None

    def finalize_capture(self) -> str:
        """Stop the live capture session and return its transcript.

        Returns the empty string if no capture was active.
        """
        cap = self.capture
        if cap is None:
            return ""
        cap.stop_event.set()
        if cap.thread is not None:
            cap.thread.join(timeout=5)
        transcript = cap.get_transcript()
        self.capture = None
        return transcript

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
