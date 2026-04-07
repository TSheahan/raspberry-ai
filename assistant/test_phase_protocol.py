"""
Run from repo root:

  python assistant/test_phase_protocol.py

Tests `phase_protocol` classification and `RecorderState` contract gating (no numpy
or Pipecat required). `set_phase` / workers live in `WiredRecorderState`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_protocol import TransitionKind, classify_transition  # noqa: E402


def test_classifier() -> None:
    import phase_protocol as pp

    pp._self_test()


def test_skipped_phases_forward() -> None:
    """wake_listen → idle skips capture on the ring; must classify as FORWARD."""
    assert classify_transition("wake_listen", "idle").kind.name == "FORWARD"


def test_recorder_core_gating() -> None:
    """RecorderState gate + commit (contract-only; no workers)."""
    from recorder_state import RecorderState

    s = RecorderState()
    assert s.phase == "dormant"
    snap = s.gate_phase_transition("wake_listen")
    assert snap is not None
    assert snap.kind == TransitionKind.CYCLE_RESET  # dormant → wake_listen (§5a)
    s.commit_phase("wake_listen")
    assert s.phase == "wake_listen"

    s.commit_phase("idle")
    stale = s.gate_phase_transition("capture")
    assert stale is not None
    assert stale.kind == TransitionKind.STALE
    assert s.phase == "idle"

    s2 = RecorderState()
    s2.commit_phase("wake_listen")
    noop = s2.gate_phase_transition("wake_listen")
    assert noop is not None
    assert noop.kind == TransitionKind.NOOP

    assert RecorderState().gate_phase_transition("not_a_phase") is None


def main() -> None:
    test_classifier()
    test_skipped_phases_forward()
    test_recorder_core_gating()
    print("test_phase_protocol OK (classifier + RecorderState core)")


if __name__ == "__main__":
    main()
