"""
recorder_state.py — Contract-first recorder child state (child side of phase_protocol).

Holds authoritative phase belief, ring write position, and frame counters. Exposes
`gate_phase_transition()` (validate + classify) and `commit_phase()` / counter
helpers used by `WiredRecorderState` after worker side-effects complete.

No pipe, SharedMemory, asyncio, Pipecat, or ONNX — testable without the Pi venv.
Downstream port (write_audio, pipe signals) and `set_phase` orchestration live in
`recorder_state_wired.WiredRecorderState`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from phase_protocol import TransitionKind, classify_transition, validate_phase


@dataclass
class PhaseTransitionSnapshot:
    """Result of gating; wired layer runs workers only when kind is actionable."""

    kind: TransitionKind
    old_phase: str
    new_phase: str


class RecorderState:
    """Authoritative recorder phase + counters (inter-process contract, child side)."""

    def __init__(self) -> None:
        self._phase = "dormant"
        self._write_pos = 0
        self._vad_frame_count = 0
        self._total_frame_count = 0

    # -----------------------------------------------------------------------
    # Phase transition gate (contract)
    # -----------------------------------------------------------------------

    def gate_phase_transition(self, new_phase: str) -> Optional[PhaseTransitionSnapshot]:
        """Classify whether ``new_phase`` may be applied.

        Returns ``None`` if ``new_phase`` is unknown. Caller logs and skips.
        STALE / NOOP / forward-like kinds are all returned; wired interprets.
        """
        if not validate_phase(new_phase):
            return None
        old = self._phase
        tc = classify_transition(old, new_phase)
        return PhaseTransitionSnapshot(kind=tc.kind, old_phase=old, new_phase=new_phase)

    def commit_phase(self, new_phase: str) -> None:
        """Set believed phase after all transition side-effects have completed."""
        self._phase = new_phase

    def apply_entry_vad_frame_reset(self, new_phase: str) -> None:
        """Reset VAD frame counter when entering wake_listen or capture (spec ordering)."""
        if new_phase in ("wake_listen", "capture"):
            self._vad_frame_count = 0

    # -----------------------------------------------------------------------
    # Ring / counters (updated by wired write_audio / processors)
    # -----------------------------------------------------------------------

    def update_write_pos(self, pos: int) -> None:
        self._write_pos = pos

    def inc_vad_frames(self) -> None:
        self._vad_frame_count += 1

    def inc_total_frames(self) -> None:
        self._total_frame_count += 1

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
