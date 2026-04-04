"""
agent_session.py — Agent subprocess abstraction (EU-6).

Two-layer design:

  AgentSession        — abstract base; defines the interface master.py uses
  CursorAgentSession  — Cursor CLI implementation (~/.local/bin/agent)

master.py integration (EU-5 Pi session):

    agent = CursorAgentSession(workspace=Path(AGENT_WORKSPACE))

    # On WAKE_DETECTED (alongside opening Deepgram WebSocket):
    agent.prepare()

    # After VAD_STOPPED + transcript assembled:
    for text_chunk in agent.run(transcript):
        tts_queue.put(text_chunk)

    # On shutdown:
    agent.close()

Session continuity: prepare() resumes the prior session if session_id is set
and time elapsed since the last turn is within resume_window_secs (default 300s,
env: AGENT_RESUME_WINDOW_SECS). Otherwise a fresh session is started.

See spec/agent_session_spec.md for full interface contract.
See memory/agent_session_patterns.md for design rationale.
"""

import json
import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

logger = logging.getLogger("agent_session")


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class AgentError(RuntimeError):
    """Raised by AgentSession.run() on subprocess failure or agent-reported error."""


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AgentSession(ABC):
    """Abstract interface for an agent subprocess session.

    Subclasses own the subprocess lifecycle and the output parsing.
    master.py only calls prepare(), run(), and close().
    """

    def __init__(self, resume_window_secs: float = 300.0) -> None:
        self._session_id: str | None = None
        self._last_turn_time: float = 0.0
        self._resume_window_secs = resume_window_secs

    # --- Public interface ---

    @abstractmethod
    def prepare(self) -> None:
        """Pre-spawn the agent subprocess on WAKE_DETECTED.

        The process starts and waits for stdin. Session continuity is
        decided here: resume if within the window, fresh otherwise.
        Safe to call multiple times (idempotent guard in subclass).
        """

    @abstractmethod
    def run(self, transcript: str) -> Iterator[str]:
        """Feed transcript to the pre-spawned process; yield TTS-safe text chunks.

        Writes transcript to stdin and closes it. Reads stdout, parses
        stream-json events, and yields word-boundary-safe text chunks.
        Updates session_id and last_turn_time on success.
        Raises AgentError on subprocess failure or is_error result.
        If prepare() was not called, calls it internally before proceeding.
        """

    def close(self) -> None:
        """Clean up any remaining subprocess state. Safe if no process is running."""

    # --- Properties ---

    @property
    def session_id(self) -> str | None:
        """Current session ID. None before first successful turn."""
        return self._session_id

    @property
    def last_turn_time(self) -> float:
        """Monotonic timestamp of last successful run(). 0.0 before first turn."""
        return self._last_turn_time

    # --- Session continuity helpers ---

    def _should_resume(self) -> bool:
        if not self._session_id:
            return False
        if self._resume_window_secs <= 0:
            return False
        return (time.monotonic() - self._last_turn_time) < self._resume_window_secs

    def _on_turn_success(self, session_id: str) -> None:
        self._session_id = session_id
        self._last_turn_time = time.monotonic()

    def _on_fresh_start(self) -> None:
        self._session_id = None


# ---------------------------------------------------------------------------
# stream-json TypedDicts (Cursor CLI schema)
# ---------------------------------------------------------------------------

class _TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class _Message(TypedDict):
    role: str
    content: list[_TextBlock]


class AssistantEvent(TypedDict):
    type: Literal["assistant"]
    message: _Message
    session_id: str
    timestamp_ms: NotRequired[int]   # present = streaming delta; absent = final duplicate


class ResultEvent(TypedDict):
    type: Literal["result"]
    subtype: str
    result: str
    session_id: str
    is_error: bool
    duration_ms: int


# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------

def parse_stream_line(line: str) -> dict | None:
    """Parse a single stdout line as JSON. Returns None on empty or decode error."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        logger.debug("stream line is not valid JSON: %r", line[:120])
        return None


def extract_delta_text(event: AssistantEvent) -> str:
    """Extract text from a streaming-delta assistant event.

    Handles multi-block content safely; returns empty string if no text found.
    """
    blocks = event.get("message", {}).get("content", [])
    return "".join(
        b.get("text", "") for b in blocks if b.get("type") == "text"
    )


# ---------------------------------------------------------------------------
# Word-boundary buffer
# ---------------------------------------------------------------------------

def _word_boundary_chunks(raw_deltas: Iterator[str]) -> Iterator[str]:
    """Buffer raw delta text and yield only word-boundary-safe chunks.

    Holds the tail of each delta after the last whitespace until the next
    delta arrives or the stream ends. Prevents a TTS engine from receiving
    a fragment that the next delta may extend mid-word.

    Treats both space and newline as flush boundaries.
    """
    buffer = ""
    for delta in raw_deltas:
        buffer += delta
        last_ws = max(buffer.rfind(" "), buffer.rfind("\n"))
        if last_ws >= 0:
            yield buffer[:last_ws + 1]
            buffer = buffer[last_ws + 1:]
    if buffer:
        yield buffer


# ---------------------------------------------------------------------------
# Cursor CLI implementation
# ---------------------------------------------------------------------------

_DEFAULT_RESUME_WINDOW = float(os.environ.get("AGENT_RESUME_WINDOW_SECS", "300"))


class CursorAgentSession(AgentSession):
    """AgentSession backed by the Cursor CLI (~/.local/bin/agent).

    Invocation:
        agent -p --output-format stream-json --stream-partial-output
              --force --yolo --trust
              --workspace <workspace> --model <model>
              [--resume <session_id>]

    stdin: receives the transcript + newline, then is closed.
    stdout: newline-delimited stream-json events.

    See spec/agent_session_spec.md for the full contract.
    See archive/2026-04-04_wrapped_cursor_agent_context.md for schema reference.
    """

    def __init__(
        self,
        workspace: Path,
        model: str = "claude-4.6-sonnet-medium",
        agent_bin: Path = Path.home() / ".local/bin/agent",
        resume_window_secs: float = _DEFAULT_RESUME_WINDOW,
    ) -> None:
        super().__init__(resume_window_secs=resume_window_secs)
        if not workspace.is_dir():
            raise ValueError(f"Agent workspace not found: {workspace}")
        self._workspace = workspace
        self._model = model
        self._agent_bin = agent_bin
        self._process: subprocess.Popen | None = None

    # --- AgentSession interface ---

    def prepare(self) -> None:
        """Spawn the agent subprocess on WAKE_DETECTED.

        Decides resume vs fresh, then spawns with stdin open. The process
        waits for input until run() closes stdin.
        """
        if self._process is not None and self._process.poll() is None:
            logger.debug("[agent] prepare() called but process already running — skipping")
            return

        resuming = self._should_resume()
        if not resuming:
            self._on_fresh_start()

        cmd = [
            str(self._agent_bin),
            "-p",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--force",
            "--yolo",
            "--trust",
            "--workspace", str(self._workspace),
            "--model", self._model,
        ]
        if resuming and self._session_id:
            cmd.extend(["--resume", self._session_id])
            logger.info("[agent] pre-spawning (resume %s...)", self._session_id[:8])
        else:
            logger.info("[agent] pre-spawning (fresh session)")

        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        logger.debug("[agent] pid=%d workspace=%s model=%s",
                     self._process.pid, self._workspace, self._model)

    def run(self, transcript: str) -> Iterator[str]:
        """Feed transcript; yield TTS-safe text chunks as the agent responds.

        Calls prepare() if the process was not pre-spawned.
        Raises AgentError on subprocess failure or agent-reported error.
        """
        if self._process is None or self._process.poll() is not None:
            logger.debug("[agent] run() called without live process — calling prepare()")
            self.prepare()

        process = self._process
        assert process is not None

        logger.debug("[agent] writing transcript to stdin (%d chars)", len(transcript))
        try:
            process.stdin.write(transcript + "\n")
            process.stdin.close()
        except BrokenPipeError as exc:
            self._process = None
            raise AgentError("agent stdin broken before transcript was written") from exc

        captured_session_id: str | None = None
        final_result: str | None = None
        raw_deltas: list[str] = []

        for raw_line in process.stdout:
            event = parse_stream_line(raw_line)
            if event is None:
                continue

            event_type = event.get("type")

            # Capture session_id from any event
            sid = event.get("session_id")
            if sid:
                captured_session_id = sid

            if event_type == "assistant" and "timestamp_ms" in event:
                delta = extract_delta_text(event)
                if delta:
                    raw_deltas.append(delta)

            elif event_type == "result":
                result_event: ResultEvent = event  # type: ignore[assignment]
                final_result = result_event.get("result", "")
                duration_ms = result_event.get("duration_ms", 0)
                usage = result_event.get("usage", {})
                if result_event.get("is_error"):
                    logger.error("[agent] is_error=true in result event: %s", final_result)
                    process.wait()
                    self._process = None
                    raise AgentError(f"agent reported error: {final_result}")
                logger.info("[agent] result ok  duration=%dms  out_tokens=%s  cache_read=%s",
                            duration_ms,
                            usage.get("outputTokens", "?"),
                            usage.get("cacheReadTokens", "?"))

        process.wait()
        self._process = None

        if process.returncode != 0:
            stderr_out = ""
            try:
                stderr_out = process.stderr.read(500).strip()
            except Exception:
                pass
            raise AgentError(
                f"agent exited with code {process.returncode}: {stderr_out}"
            )

        if captured_session_id:
            self._on_turn_success(captured_session_id)
            logger.debug("[agent] session_id=%s", captured_session_id[:8])

        yield from _word_boundary_chunks(iter(raw_deltas))

    def close(self) -> None:
        """Terminate any live agent subprocess."""
        if self._process is None:
            return
        if self._process.poll() is None:
            logger.debug("[agent] terminating subprocess pid=%d", self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning("[agent] terminate timed out — killing pid=%d",
                               self._process.pid)
                self._process.kill()
        self._process = None
