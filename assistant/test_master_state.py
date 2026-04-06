"""Run: python assistant/test_master_state.py (from repo root)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from master_state import MasterState  # noqa: E402


def test_stale_state_changed() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    s.on_state_changed("idle")
    assert s.phase == "idle"
    s.on_state_changed("capture")
    assert s.phase == "idle"


def test_wake_gated() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    assert s.on_wake_detected(1, 0.9, "ok")
    assert s.wake_pos == 1
    s.processing = True
    assert not s.on_wake_detected(2, 0.9, "ok")


def test_vad_requires_capture_belief_and_live_session() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    assert not s.on_vad_started(0)
    s.on_state_changed("capture")
    assert not s.on_vad_started(0)
    s.capture = object()
    assert s.on_vad_started(0)
    assert s.on_vad_stopped(1)


def test_orphan_vad_stopped_rejected() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    s.on_state_changed("capture")
    s.capture = object()
    assert not s.on_vad_stopped(0)


def test_fast_forward_wake_to_idle_tears_down_capture() -> None:
    class Cap:
        def __init__(self) -> None:
            import threading

            self.stop_event = threading.Event()
            self.thread = None

        def get_transcript(self) -> str:
            return ""

    s = MasterState()
    s.on_state_changed("wake_listen")
    cap = Cap()
    s.capture = cap
    s.on_state_changed("idle")
    assert s.capture is None
    assert cap.stop_event.is_set()


def test_stt_pending_cleared_on_idle_entry() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    s.stt_start_pending = True
    s.on_state_changed("idle")
    assert not s.stt_start_pending


def test_agent_prepare_tracking() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    assert not s.agent_prepare_done
    s.note_agent_prepare()
    assert s.agent_prepare_done
    s.on_state_changed("idle")
    s.on_state_changed("wake_listen")
    assert not s.agent_prepare_done


def test_note_agent_prepare_idempotent_after_double_call() -> None:
    s = MasterState()
    s.on_state_changed("wake_listen")
    s.note_agent_prepare()
    s.note_agent_prepare()
    assert s.agent_prepare_done


def main() -> None:
    test_stale_state_changed()
    test_wake_gated()
    test_vad_requires_capture_belief_and_live_session()
    test_orphan_vad_stopped_rejected()
    test_fast_forward_wake_to_idle_tears_down_capture()
    test_stt_pending_cleared_on_idle_entry()
    test_agent_prepare_tracking()
    test_note_agent_prepare_idempotent_after_double_call()
    print("test_master_state OK")


if __name__ == "__main__":
    main()
