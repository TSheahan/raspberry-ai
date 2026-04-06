"""
phase_protocol.py — Shared inter-process phase contract (master_state_spec.md §5a).

Single source for legal phase names, cycle ordering, and transition classification.
Imported by RecorderState (child) and later by MasterState (master).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TransitionKind(Enum):
    """Result of classifying old_phase → new_phase."""

    NOOP = "noop"
    FORWARD = "forward"
    CYCLE_RESET = "cycle_reset"  # → wake_listen (from any non–wake_listen phase)
    TO_DORMANT = "to_dormant"
    STALE = "stale"


# Legal phase vocabulary (wire strings / RecorderState._phase).
PHASES: frozenset[str] = frozenset(
    {"dormant", "wake_listen", "capture", "idle"}
)

# Linear order within the repeating turn (dormant is outside this ring).
PHASE_CYCLE: tuple[str, ...] = ("wake_listen", "capture", "idle")

_PHASE_IDX: dict[str, int] = {p: i for i, p in enumerate(PHASE_CYCLE)}


@dataclass(frozen=True)
class TransitionClass:
    kind: TransitionKind


def validate_phase(name: str) -> bool:
    return name in PHASES


def classify_transition(current: str, proposed: str) -> TransitionClass:
    """Classify a phase change per master_state_spec.md §5a.

    - NOOP: same phase (side-effects do not re-fire; caller may still re-signal).
    - CYCLE_RESET: proposed is wake_listen (valid from any other phase).
    - TO_DORMANT: proposed is dormant (valid from any non-dormant phase).
    - FORWARD: both on the cycle ring and proposed is strictly after current
      along wake_listen → capture → idle (skipping intermediates allowed).
    - STALE: backward along the ring toward an earlier phase, unless target
      is wake_listen (handled as CYCLE_RESET).
    """
    if current not in PHASES or proposed not in PHASES:
        return TransitionClass(TransitionKind.STALE)
    if current == proposed:
        return TransitionClass(TransitionKind.NOOP)
    if proposed == "dormant":
        return TransitionClass(TransitionKind.TO_DORMANT)
    if current == "dormant":
        # Only wake_listen may follow dormant (enter the active cycle).
        if proposed == "wake_listen":
            return TransitionClass(TransitionKind.CYCLE_RESET)
        return TransitionClass(TransitionKind.STALE)
    if proposed == "wake_listen":
        return TransitionClass(TransitionKind.CYCLE_RESET)

    i = _PHASE_IDX.get(current)
    j = _PHASE_IDX.get(proposed)
    if i is None or j is None:
        return TransitionClass(TransitionKind.STALE)
    if j > i:
        return TransitionClass(TransitionKind.FORWARD)
    return TransitionClass(TransitionKind.STALE)


def _self_test() -> None:
    c = classify_transition
    T = TransitionKind

    assert c("dormant", "dormant").kind == T.NOOP
    assert c("wake_listen", "wake_listen").kind == T.NOOP

    assert c("dormant", "wake_listen").kind == T.CYCLE_RESET
    assert c("dormant", "capture").kind == T.STALE
    assert c("dormant", "idle").kind == T.STALE

    assert c("wake_listen", "capture").kind == T.FORWARD
    assert c("capture", "idle").kind == T.FORWARD
    assert c("wake_listen", "idle").kind == T.FORWARD

    assert c("idle", "wake_listen").kind == T.CYCLE_RESET
    assert c("capture", "wake_listen").kind == T.CYCLE_RESET

    assert c("idle", "capture").kind == T.STALE

    assert c("wake_listen", "dormant").kind == T.TO_DORMANT
    assert c("capture", "dormant").kind == T.TO_DORMANT
    assert c("idle", "dormant").kind == T.TO_DORMANT

    assert c("bogus", "wake_listen").kind == T.STALE
    assert c("wake_listen", "bogus").kind == T.STALE


if __name__ == "__main__":
    _self_test()
    print("phase_protocol self-test OK")
