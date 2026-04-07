"""
master_state_wired.py — WiredMasterState: MasterState + pipe, workers, STT thread, cognitive loop.

Pure phase/VAD logic stays in MasterState; this module holds side-effect orchestration
that master_loop used to own. See master_state.py for the testable base class.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from deepgram import DeepgramClient
from deepgram.core.events import EventType
from loguru import logger

from agent_session import AgentError, AgentSession
from audio_shm_ring import CHANNELS, SAMPLE_RATE, AudioShmRingReader
from master_state import MasterState, StateChangeResult
from tts_backends import TTSBackend

_log = logger.bind(name="master")

# Deepgram keepalive: send every N seconds when ring write_pos has not advanced.
# Deepgram closes the WebSocket with NET-0001 after 10 s of silence.
_DG_KEEPALIVE_INTERVAL = 3.5


# ---------------------------------------------------------------------------
# Streaming capture session — ring tail + Deepgram live WebSocket
# ---------------------------------------------------------------------------


class _SttCaptureSession:
    """State for one WAKE_DETECTED → VAD_STOPPED STT (Deepgram) capture window."""

    def __init__(self) -> None:
        self._transcripts: list[str] = []
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def add_transcript(self, text: str) -> None:
        with self._lock:
            self._transcripts.append(text)

    def get_transcript(self) -> str:
        with self._lock:
            return " ".join(self._transcripts)


# FUTURE: could become a WiredMasterState method if the threading model changes.
def _run_capture(
    capture: _SttCaptureSession,
    ring_reader: AudioShmRingReader,
    wake_pos: int,
    dg_client: DeepgramClient,
) -> None:
    """Ring-tail + Deepgram live session. Runs in a thread from WAKE_DETECTED to VAD_STOPPED.

    Opens a Deepgram live WebSocket, tails the ring buffer at ~20 ms intervals,
    and accumulates is_final transcripts in capture.  On stop_event, flushes
    remaining audio, sends send_finalize(), and closes the connection.
    """

    def on_message(message) -> None:
        if getattr(message, "type", None) != "Results":
            return
        if not message.is_final:
            return
        # Note: post-CloseStream Results with from_finalize=false have been
        # observed in practice (EU-5 runs 2026-04-04) and have been consistently
        # empty. Handled correctly by the is_final guard above; no action needed.
        try:
            text = message.channel.alternatives[0].transcript.strip()
            if text:
                capture.add_transcript(text)
                _log.debug("[dg-live] is_final: {!r}", text)
        except Exception as exc:
            _log.debug("[dg-live] on_message parse error: {}", exc)

    def on_error(error) -> None:
        _log.error("[dg-live] error: {}", error)

    try:
        with dg_client.listen.v1.connect(
            model="nova-3",
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            language="en-US",
            smart_format="true",
            interim_results="true",
            endpointing=300,
        ) as conn:
            conn.on(EventType.MESSAGE, on_message)
            conn.on(EventType.ERROR, on_error)
            listen_thread = threading.Thread(target=conn.start_listening, daemon=True)
            listen_thread.start()

            pos = wake_pos
            last_keepalive = time.monotonic()

            while not capture.stop_event.is_set():
                new_wp = ring_reader.write_pos
                if new_wp != pos:
                    chunk = ring_reader.read(pos, new_wp)
                    if chunk:
                        conn.send_media(chunk)
                        last_keepalive = time.monotonic()
                    pos = new_wp
                elif time.monotonic() - last_keepalive >= _DG_KEEPALIVE_INTERVAL:
                    conn.send_keep_alive()
                    last_keepalive = time.monotonic()
                    _log.debug("[dg-live] keepalive sent")
                time.sleep(0.02)

            # Flush any frames written between the last poll and stop_event.
            new_wp = ring_reader.write_pos
            if new_wp != pos:
                chunk = ring_reader.read(pos, new_wp)
                if chunk:
                    conn.send_media(chunk)

            conn.send_finalize()
            # 200ms pause lets Deepgram flush trailing results before CloseStream.
            # This blocks master for ~460ms total (sleep + WS close + listen join)
            # before SET_IDLE and cognitive_loop can start. A potential optimisation
            # is to feed the transcript to the agent immediately at VAD_STOPPED and
            # skip waiting for finalize results. Timing analysis (EU-5 runs 2026-04-04)
            # shows this is low-risk — the definitive is_final arrives well before
            # VAD_STOPPED in practice — but agents do not accept supplemental input
            # while responding, so any trailing Deepgram segment would be silently
            # lost. Adopt only once the chance of trailing-text loss is satisfactorily
            # characterised across longer utterances.
            time.sleep(0.2)
            conn.send_close_stream()
            listen_thread.join(timeout=2)

    except Exception as exc:
        _log.error("[dg-live] capture session error: {}", exc)


# FUTURE: could become a method to read workers from self instead of parameters.
def cognitive_loop(transcript: str, agent: AgentSession, tts: TTSBackend) -> None:
    """Feed transcript to agent; synthesise and play each yielded sentence chunk."""
    if not transcript:
        _log.warning("no transcript — skipping cognitive loop")
        return
    _log.info("TRANSCRIPT: {}", transcript)
    loop_start = time.monotonic()
    try:
        # Warm overlaps agent time-to-first-token (not at wake — avoids priming TTS
        # during long dictation-only idle after wake).
        threading.Thread(target=tts.warm, daemon=True).start()
        tts.play(agent.run(transcript))
    except AgentError as exc:
        _log.error("[agent] error: {}", exc)
    _log.info("cognitive loop latency: {:.2f}s", time.monotonic() - loop_start)


class WiredMasterState(MasterState):
    """MasterState with pipe, agent, TTS, ring reader, and Deepgram client wired in.

    Workers are held by strong references; none hold back-references to this object
    today (same rationale as RecorderState._pipe). FUTURE: use weakref for a worker
    that gains a back-reference to state or outlives the master less than process
    lifetime.

    TODO: all workers are constructed before the state in master_loop — constructor
    injection could replace post-construction set_* calls.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pipe: Any = None
        self._agent: AgentSession | None = None
        self._tts: TTSBackend | None = None
        self._ring_reader: AudioShmRingReader | None = None
        self._dg_client: DeepgramClient | None = None

    def set_pipe(self, pipe: Any) -> None:
        self._pipe = pipe

    def set_agent(self, agent: AgentSession) -> None:
        self._agent = agent

    def set_tts(self, tts: TTSBackend) -> None:
        self._tts = tts

    def set_ring_reader(self, ring_reader: AudioShmRingReader) -> None:
        self._ring_reader = ring_reader

    def set_dg_client(self, dg_client: DeepgramClient) -> None:
        self._dg_client = dg_client

    def _arm_stt_session(self) -> None:
        """Start Deepgram + ring tail once belief is capture (STATE_CHANGED) and SET_CAPTURE was sent."""
        assert self._ring_reader is not None and self._dg_client is not None
        cap = _SttCaptureSession()
        wake_pos = self.arm_stt(cap)
        if wake_pos < 0:
            return
        cap.thread = threading.Thread(
            target=_run_capture,
            args=(cap, self._ring_reader, wake_pos, self._dg_client),
            daemon=True,
        )
        cap.thread.start()

    def on_wake_detected(self, write_pos: int, score: float, keyword: str) -> bool:
        if not super().on_wake_detected(write_pos, score, keyword):
            return False
        assert self._agent is not None and self._pipe is not None
        _log.info("[master] WAKE_DETECTED  score={:.3f}  keyword={}", score, keyword)
        # Pre-spawn agent (hides startup latency behind the STT window).
        # Deepgram + ring tail start on STATE_CHANGED(capture) (master_state_spec §2d).
        self._agent.prepare()
        self.note_agent_prepare()
        self._pipe.send({"cmd": "SET_CAPTURE"})
        self.mark_stt_pending_after_set_capture()
        return True

    def on_state_changed(self, new_phase: str) -> StateChangeResult:
        res = super().on_state_changed(new_phase)
        if res.accepted and self.stt_arm_ready:
            self._arm_stt_session()
            print("!! SPEAK !!", flush=True)
        elif res.accepted and self.capture_phase_without_pending_stt:
            _log.warning(
                "[master] STATE_CHANGED(capture) but STT not pending — "
                "possible protocol skew",
            )
        return res

    def on_vad_stopped(self, write_pos: int) -> bool:
        if not super().on_vad_stopped(write_pos):
            _log.debug(
                "[master] VAD_STOPPED ignored (phase={} vad_speaking={})",
                self.phase,
                self.vad_speaking,
            )
            return False
        assert self._pipe is not None and self._agent is not None and self._tts is not None
        _log.info("[master] VAD_STOPPED    write_pos={}", write_pos)
        transcript = self.finalize_capture()
        self._pipe.send({"cmd": "SET_IDLE"})
        self.begin_processing()
        try:
            cognitive_loop(transcript, self._agent, self._tts)
        except Exception as exc:
            _log.error("cognitive loop error: {}", exc)
        finally:
            self.end_processing()
            try:
                self._pipe.send({"cmd": "SET_WAKE_LISTEN"})
            except (BrokenPipeError, OSError):
                pass
            _log.info("listening for wake word...")
        return True

    def close(self) -> None:
        """Teardown capture and close workers (subclass owns lifecycle)."""
        self.teardown_capture()
        if self._agent is not None:
            self._agent.close()
        if self._tts is not None:
            self._tts.close()
