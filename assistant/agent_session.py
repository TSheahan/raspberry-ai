"""
agent_session.py — Agent subprocess abstraction (EU-6).

Two-layer design:

  AgentSession        — abstract base; defines the interface voice_assistant.py uses
  CursorAgentSession  — Cursor CLI implementation (~/.local/bin/agent)

voice_assistant.py integration (EU-5 Pi session):

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
import os
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from loguru import logger


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
    voice_assistant.py only calls prepare(), run(), and close().
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
        """Feed transcript to the pre-spawned process; yield TTS-safe sentence chunks.

        Writes transcript to stdin and closes it. Reads stdout, parses
        stream-json events, and yields sentence-boundary-safe text chunks live
        as streaming deltas arrive. Any tail text not covered by live sentences
        is yielded from result.result after the stream closes.
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
        logger.warning("[agent/stdout] not valid JSON (stream-json expected): {!r}", line[:200])
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
# Sentence-boundary buffer
# ---------------------------------------------------------------------------

# Matches a sentence-ending punctuation mark followed by whitespace or
# end-of-string. The look-ahead keeps the punctuation in the yielded chunk.
_SENTENCE_BOUNDARY = re.compile(r'[.!?](?=\s|$)')


def _flush_sentences(buffer: str) -> tuple[list[str], str]:
    """Extract all complete sentences from buffer; return (sentences, remainder).

    A sentence ends at the first [.!?] followed by whitespace or end-of-string.
    Leading whitespace is stripped from each sentence and from the remainder.
    """
    sentences: list[str] = []
    while True:
        m = _SENTENCE_BOUNDARY.search(buffer)
        if not m:
            break
        sentence = buffer[:m.end()].strip()
        buffer = buffer[m.end():].lstrip()
        if sentence:
            sentences.append(sentence)
    return sentences, buffer


# ---------------------------------------------------------------------------
# Cursor CLI implementation
# ---------------------------------------------------------------------------

_DEFAULT_RESUME_WINDOW = float(os.environ.get("AGENT_RESUME_WINDOW_SECS", "300"))

# When set, the agent subprocess is launched as this Linux user via
# `sudo -u <AGENT_USER> -H --`. Requires a sudoers entry, e.g.:
#   voice ALL=(agent) NOPASSWD: /home/agent/artifacts/cursor-agent-wrapper
#   (or the raw CLI path if no wrapper)
# Leave unset (default) to run the agent as the current process user.
_AGENT_USER = os.environ.get("AGENT_USER", "")


class CursorAgentSession(AgentSession):
    """AgentSession backed by the Cursor CLI (~/.local/bin/agent).

    Invocation:
        agent -p --output-format stream-json --stream-partial-output
              --force --yolo --trust
              --workspace <workspace> --model <model>
              [--resume <session_id>]

    stdin: receives the transcript + newline, then is closed.
    stdout: newline-delimited stream-json events.

    **Argv vs transcript:** The STT transcript is written only to **stdin**, not appended to argv.
    A supervising wrapper may log argv without expecting user prompts or secrets there — do not
    move transcript text onto the command line.

    See spec/agent_session_spec.md for the full contract.
    See archive/2026-04-04_wrapped_cursor_agent_context.md for schema reference.
    See spec/cursor_agent_wrapper_spec.md §6 for logging policy alignment.
    """

    def __init__(
        self,
        workspace: Path,
        model: str = "claude-4.6-sonnet-medium",
        agent_bin: Path = Path.home() / ".local/bin/agent",
        resume_window_secs: float = _DEFAULT_RESUME_WINDOW,
    ) -> None:
        super().__init__(resume_window_secs=resume_window_secs)
        try:
            exists = workspace.is_dir()
        except PermissionError:
            exists = True  # path exists but is not readable by this process (expected with AGENT_USER)
        if not exists:
            raise ValueError(f"Agent workspace not found: {workspace}")
        self._workspace = workspace
        self._model = model
        self._agent_bin = agent_bin
        self._process: subprocess.Popen | None = None
        self._last_spawn_used_resume: bool = False

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
        self._last_spawn_used_resume = bool(resuming and self._session_id)
        if not resuming:
            self._on_fresh_start()

        agent_args = [
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
        if self._last_spawn_used_resume and self._session_id:
            agent_args.extend(["--resume", self._session_id])
            logger.info("[agent] pre-spawning (resume {}...)", self._session_id[:8])
        else:
            logger.info("[agent] pre-spawning (fresh session)")

        # Prefix with sudo when AGENT_USER is configured so the subprocess runs
        # as the dedicated agent Linux user. The "--" prevents sudo from
        # interpreting subsequent flags (e.g. --resume) as its own options.
        cmd = (["sudo", "-u", _AGENT_USER, "-H", "--"] + agent_args) if _AGENT_USER else agent_args

        # start_new_session gives the sudo→wrapper tree its own session.
        # The wrapper internally uses setsid (with fd3 stdin preservation)
        # to place the real CLI in a further sub-group for targeted killpg.
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        logger.debug("[agent] pid={} workspace={} model={}",
                     self._process.pid, self._workspace, self._model)
        logger.debug("[agent] argv: {}", cmd)

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

        logger.debug("[agent] writing transcript to stdin ({} chars)", len(transcript))
        try:
            process.stdin.write(transcript + "\n")
            process.stdin.close()
        except BrokenPipeError as exc:
            self._process = None
            raise AgentError("agent stdin broken before transcript was written") from exc

        logger.info(
            "[agent] stdin closed — waiting for stream-json on stdout  pid={}  spawn_used_resume={}  prior_session_id={}",
            process.pid,
            self._last_spawn_used_resume,
            (self._session_id[:12] + "…") if self._session_id else "—",
        )

        captured_session_id: str | None = None
        final_result: str | None = None
        delta_count = 0
        first_event_logged = False

        # Live sentence streaming state.
        # sentence_buffer accumulates incoming delta text until a sentence
        # boundary is found. yielded_text tracks everything already spoken
        # so the end-of-stream tail logic can avoid double-speaking.
        sentence_buffer = ""
        yielded_text = ""

        stderr_stop = threading.Event()

        def _drain_stderr() -> None:
            err = process.stderr
            if err is None:
                return
            try:
                for line in iter(err.readline, ""):
                    if stderr_stop.is_set():
                        break
                    line = line.rstrip()
                    if line:
                        logger.warning("[agent/stderr] {}", line)
            except Exception:
                logger.exception("[agent] stderr reader failed")

        stderr_thread = threading.Thread(target=_drain_stderr, name="agent-stderr", daemon=True)
        stderr_thread.start()

        try:
            for raw_line in process.stdout:
                event = parse_stream_line(raw_line)
                if event is None:
                    continue

                if not first_event_logged:
                    first_event_logged = True
                    logger.info("[agent] first stdout line parsed — stream-json flow started")

                event_type = event.get("type")
                has_ts = "timestamp_ms" in event
                short_sid = (event.get("session_id") or "")[:8] or "-"
                logger.debug("[agent/evt] type={!r} has_ts={} sid={}", event_type, has_ts, short_sid)

                # Capture session_id from any event
                sid = event.get("session_id")
                if sid:
                    captured_session_id = sid

                if event_type == "assistant":
                    delta = extract_delta_text(event)
                    logger.debug("[agent/evt] assistant has_ts={} text={!r}", has_ts, delta[:120])
                    if has_ts and delta:
                        # Re-emission guard: after a tool call the CLI resends the
                        # pre-tool text as a single timestamped delta. Signature:
                        # buffer is empty (just cleared after a yield), something
                        # was already yielded, and the delta exactly matches it.
                        # Skip to avoid double-speaking the pre-tool sentence.
                        if (not sentence_buffer
                                and yielded_text
                                and delta.strip() == yielded_text.strip()):
                            logger.debug("[agent] re-emission detected — skipping: {!r}",
                                         delta[:60])
                            continue
                        delta_count += 1
                        sentence_buffer += delta
                        sentences, sentence_buffer = _flush_sentences(sentence_buffer)
                        for sentence in sentences:
                            yielded_text += sentence + " "
                            yield sentence

                elif event_type == "result":
                    result_event: ResultEvent = event  # type: ignore[assignment]
                    final_result = result_event.get("result", "")
                    duration_ms = result_event.get("duration_ms", 0)
                    usage = result_event.get("usage", {})
                    logger.debug("[agent/evt] result is_error={} result_len={} deltas={}",
                                 result_event.get("is_error"), len(final_result), delta_count)
                    if result_event.get("is_error"):
                        logger.error("[agent] is_error=true in result event: {}", final_result)
                        process.wait()
                        self._process = None
                        raise AgentError(f"agent reported error: {final_result}")
                    logger.info("[agent] result ok  duration={}ms  out_tokens={}  cache_read={}",
                                duration_ms,
                                usage.get("outputTokens", "?"),
                                usage.get("cacheReadTokens", "?"))

                else:
                    logger.debug("[agent/evt] ignored type={!r}", event_type)
        finally:
            stderr_stop.set()
            stderr_thread.join(timeout=2)

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
            logger.debug("[agent] session_id={}", captured_session_id[:8])

        # Tail reconciliation against result.result (canonical ground truth).
        #
        # Three cases:
        #   1. Normal: yielded_text is a clean prefix of result.result → yield tail.
        #   2. Nothing yielded live (response too short to hit a sentence boundary,
        #      or no timestamped deltas) → yield sentence_buffer then result.result.
        #   3. Mismatch (tool-call re-emission shifted the delta text) → flush
        #      sentence_buffer only; do not double-speak result.result.
        if final_result is not None:
            canonical = final_result.strip()
            already = yielded_text.strip()
            if already and canonical.startswith(already):
                tail = canonical[len(already):].strip()
                if tail:
                    logger.debug("[agent] yielding tail ({} chars)", len(tail))
                    yield tail
                else:
                    logger.debug("[agent] live sentences covered full result.result — no tail")
            elif not already:
                leftover = sentence_buffer.strip() or canonical
                if leftover:
                    logger.debug("[agent] no live sentences — yielding ({} chars)", len(leftover))
                    yield leftover
            else:
                logger.debug("[agent] result.result mismatch — flushing buffer (re-emission?)")
                if sentence_buffer.strip():
                    yield sentence_buffer.strip()
        elif sentence_buffer.strip():
            logger.warning("[agent] no result event — flushing buffer ({} chars)",
                           len(sentence_buffer))
            yield sentence_buffer.strip()

    def close(self) -> None:
        """Terminate any live agent subprocess."""
        if self._process is None:
            return
        if self._process.poll() is None:
            logger.debug("[agent] terminating subprocess pid={}", self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning("[agent] terminate timed out — killing pid={}",
                               self._process.pid)
                self._process.kill()
        self._process = None
