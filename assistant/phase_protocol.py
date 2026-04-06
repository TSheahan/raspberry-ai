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


def exit_phases_for_belief_update(belief: str, reported: str) -> list[str]:
    """Phases whose master-side **exit** hooks must run, in order, when belief
    updates from ``belief`` to ``reported`` (master_state_spec.md §4c).

    Call only when ``classify_transition`` is not STALE/NOOP. ``TO_DORMANT``
    exits the current phase only. ``CYCLE_RESET`` → ``wake_listen`` from
    ``capture`` runs ``capture`` then ``idle`` exits (``idle`` exit is a no-op
    today). ``FORWARD`` on the ring exits ``belief`` then each strictly
    intermediate phase up to ``reported``.
    """
    tc = classify_transition(belief, reported)
    if tc.kind in (TransitionKind.STALE, TransitionKind.NOOP):
        return []

    if tc.kind == TransitionKind.TO_DORMANT:
        if belief == "dormant":
            return []
        return [belief]

    if tc.kind == TransitionKind.FORWARD:
        ib = _PHASE_IDX[belief]
        ir = _PHASE_IDX[reported]
        out: list[str] = [belief]
        out.extend(PHASE_CYCLE[ib + 1 : ir])
        return out

    if tc.kind == TransitionKind.CYCLE_RESET and reported == "wake_listen":
        if belief == "dormant":
            return []
        if belief == "idle":
            return ["idle"]
        if belief == "capture":
            return ["capture", "idle"]
        if belief == "wake_listen":
            return []
        return []

    return []


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

    x = exit_phases_for_belief_update
    assert x("wake_listen", "capture") == ["wake_listen"]
    assert x("capture", "idle") == ["capture"]
    assert x("wake_listen", "idle") == ["wake_listen", "capture"]
    assert x("capture", "wake_listen") == ["capture", "idle"]
    assert x("idle", "wake_listen") == ["idle"]
    assert x("dormant", "wake_listen") == []
    assert x("capture", "dormant") == ["capture"]


if __name__ == "__main__":
    _self_test()
    print("phase_protocol self-test OK")
