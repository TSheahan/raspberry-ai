"""
Run from repo root:

  python assistant/test_phase_protocol.py

The classifier tests always run. RecorderState integration tests require
`numpy` (pulled in by `recorder_state.py`) — e.g. the Pi venv.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_protocol import classify_transition  # noqa: E402


def test_classifier() -> None:
    import phase_protocol as pp

    pp._self_test()


def _run_recorder_harness_tests() -> None:
    """Requires numpy (see recorder_state imports)."""
    from recorder_state import RecorderState

    class _RecorderStateHarness(RecorderState):
        def __init__(self) -> None:
            super().__init__(pipe=None, shm=None)
            self.state_changed_count = 0

        def signal_state_changed(self) -> None:
            self.state_changed_count += 1

        def signal_wake_detected(self, score: float, keyword: str) -> None:
            pass

        def signal_vad_started(self) -> None:
            pass

        def signal_vad_stopped(self) -> None:
            pass

        def write_audio(self, frame_bytes: bytes) -> None:
            pass

    async def test_recorder_rejects_stale() -> None:
        s = _RecorderStateHarness()
        assert s.phase == "dormant"
        await s.set_phase("wake_listen")
        assert s.phase == "wake_listen"
        await s.set_phase("idle")
        assert s.phase == "idle"
        n = s.state_changed_count
        await s.set_phase("capture")
        assert s.phase == "idle"
        assert s.state_changed_count == n

    async def test_recorder_noop_signals() -> None:
        s = _RecorderStateHarness()
        await s.set_phase("wake_listen")
        n = s.state_changed_count
        await s.set_phase("wake_listen")
        assert s.state_changed_count == n + 1

    asyncio.run(test_recorder_rejects_stale())
    asyncio.run(test_recorder_noop_signals())


def test_skipped_phases_forward() -> None:
    """wake_listen → idle skips capture on the ring; must classify as FORWARD."""
    assert classify_transition("wake_listen", "idle").kind.name == "FORWARD"


def main() -> None:
    test_classifier()
    test_skipped_phases_forward()
    try:
        import numpy  # noqa: F401
    except ImportError:
        print(
            "Skipping RecorderState tests (numpy not installed — use Pi venv for full run)"
        )
        print("test_phase_protocol OK (classifier)")
        return
    _run_recorder_harness_tests()
    print("test_phase_protocol OK (classifier + RecorderState)")


if __name__ == "__main__":
    main()
