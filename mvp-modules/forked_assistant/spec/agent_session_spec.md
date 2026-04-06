# Agent Session Specification

**Date:** 2026-04-04
**Status:** Design spec — `src/agent_session.py` implements this contract
**Parent:** `forked_assistant/spec/implementation_framework.md` (EU-6)

---

## Overview

`agent_session.py` provides a two-layer abstraction for the agent subprocess that drives the cognitive response path in `master.py`:

- **`AgentSession`** — abstract base class. Defines the interface `master.py` uses. Independent of any specific agent backend.
- **`CursorAgentSession(AgentSession)`** — Cursor CLI implementation. Uses `~/.local/bin/agent` with `stream-json` output. Proven on Pi ARM64 (2026-04-04 smoke test).

The interchangeability layer is the primary motivation for the abstraction: at the current rate of AI tooling change, the backend (Cursor CLI, Claude CLI, direct API, future alternatives) must be swappable without touching `master.py`.

---

## `AgentSession` — Abstract Interface

### Constructor

Subclasses define their own constructors. No abstract constructor is enforced.

### Methods

#### `prepare() -> None`

Pre-spawn the agent subprocess. Called on WAKE_DETECTED, before the transcript is available. The process starts and waits for stdin input.

- Decides resume vs fresh based on `last_turn_time` and `resume_window_secs`:
  - If `session_id` is set **and** `time.monotonic() - last_turn_time < resume_window_secs`: resume
  - Otherwise: fresh start, `session_id` cleared
- Must be safe to call if already prepared (idempotent guard).
- If `prepare()` was not called before `run()`, `run()` calls it internally.

#### `run(transcript: str) -> Iterator[str]`

Feed the completed transcript to the pre-spawned process; iterate word-boundary-safe text chunks for the TTS pipeline.

- Writes `transcript + "\n"` to the process's stdin, then closes stdin.
- Reads stdout line by line, parses each JSON event.
- Yields text chunks from `assistant` delta events (those with `timestamp_ms`). Each yielded string ends at a whitespace boundary — the TTS engine will not receive a fragment that could be extended mid-word by the next chunk.
- Updates `last_turn_time` and `session_id` on successful completion.
- Raises `AgentError` on non-zero subprocess exit or on `is_error: true` in the `result` event.
- Flushes the word-boundary buffer (any retained tail) at stream end before returning.

#### `close() -> None`

Clean up any remaining subprocess state. Called by `master.py` on shutdown. Safe to call if no process is running.

### Properties

#### `session_id -> str | None`

Current session ID. `None` before the first successful turn. Set from the first `session_id` field seen in the stream-json output. Cleared when a fresh session is started.

#### `last_turn_time -> float`

Monotonic clock timestamp of the most recent successful `run()` completion. `0.0` before the first turn. Used by `prepare()` to decide resume vs fresh.

### Exceptions

#### `AgentError(RuntimeError)`

Raised by `run()` on:
- Subprocess non-zero exit code
- `result.is_error == True` in the stream output
- Pipe broken mid-stream (subprocess crash)

`master.py` catches `AgentError`, logs it, and continues to the next wake cycle without retrying.

---

## `CursorAgentSession(AgentSession)` — Cursor CLI Implementation

### Constructor

```python
CursorAgentSession(
    workspace: Path,
    model: str = "claude-4.6-sonnet-medium",
    agent_bin: Path = Path.home() / ".local/bin/agent",
    resume_window_secs: float = float(os.environ.get("AGENT_RESUME_WINDOW_SECS", "300")),
)
```

`workspace` is required. Must be an existing directory — the agent loads `AGENTS.md`, project rules, and memory files from it at startup.

### Subprocess Invocation

```
agent -p
      --output-format stream-json
      --stream-partial-output
      --force
      --yolo
      --trust
      --workspace <workspace>
      --model <model>
      [--resume <session_id>]
```

Spawned with `subprocess.Popen(stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, bufsize=1)`.

Flag rationale:
- `--output-format stream-json --stream-partial-output`: newline-delimited JSON with incremental token deltas
- `--force / --yolo`: bypass tool execution confirmations (required for unattended voice operation)
- `--trust`: trust workspace without interactive prompt (required for headless `-p` mode)
- `--resume`: omitted on fresh start; included with captured `session_id` within the resume window

**Security / argv:** The user utterance is sent on **stdin** (`transcript + "\n"`), not on the process argv. Command-line arguments are Cursor flags and paths only. Callers must **keep** that invariant if a future wrapper logs argv for diagnostics — see `cursor_agent_wrapper_spec.md` §6.

### stream-json Event Schema

Three event types are consumed. All other types (`user` echo, tool calls, etc.) are ignored.

#### `system` / `init`

```json
{"type": "system", "subtype": "init", "session_id": "uuid", "model": "...", "cwd": "..."}
```

Captures `session_id`. No other action.

#### `assistant` delta

```json
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Two"}]}, "session_id": "...", "timestamp_ms": 1775269273752}
```

**Discriminator: `timestamp_ms` present.** Extract `message.content[0].text` and pass through the word-boundary buffer.

#### `assistant` final (skip)

```json
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Two cats!"}]}, "session_id": "..."}
```

**Discriminator: `timestamp_ms` absent.** This is a duplicate of all accumulated deltas. Skip for accumulation; `result.result` is used as the canonical final string.

#### `result`

```json
{"type": "result", "subtype": "success", "result": "...", "session_id": "...", "is_error": false, "duration_ms": 3007, "usage": {...}}
```

Terminal event. On `is_error: true`, raise `AgentError`. On success, log duration and usage, update `session_id` and `last_turn_time`.

### TypedDict Definitions

Local TypedDicts for the three consumed event types. No external dependency.

```python
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
    timestamp_ms: NotRequired[int]

class ResultEvent(TypedDict):
    type: Literal["result"]
    subtype: str
    result: str
    session_id: str
    is_error: bool
    duration_ms: int
```

### Word-Boundary Buffer Algorithm

```python
buffer = ""
for delta_text in raw_delta_stream:
    buffer += delta_text
    last_ws = max(buffer.rfind(" "), buffer.rfind("\n"))
    if last_ws >= 0:
        yield buffer[:last_ws + 1]
        buffer = buffer[last_ws + 1:]
if buffer:
    yield buffer  # flush tail at stream end
```

`\n` is treated as a whitespace boundary equivalent to space. The caller (TTS pipeline) receives only complete word-ending chunks until the final flush.

---

## Session Continuity Policy

```
prepare() called
    │
    ├── session_id is None → fresh start
    ├── elapsed >= resume_window_secs → clear session_id, fresh start
    └── elapsed < resume_window_secs → --resume session_id
```

`resume_window_secs` defaults to 300s (5 min) from `AGENT_RESUME_WINDOW_SECS` env var. Setting to `0` disables resume entirely (always fresh). No upper bound is enforced — very large values effectively make sessions permanent until the window is explicitly reduced.

**Forward extension (not in scope for EU-6):** A `FORCE_NEW_SESSION` pipe command from master could force a fresh start on demand (e.g., user explicitly says "start over"). This would call `close()` on the current session and clear `session_id` before the next `prepare()`.

---

## Integration with `master.py` (EU-5 Pi Session)

The changes to `master.py` that wire in `AgentSession` are deferred to the EU-5 Pi session. The interface is designed for a clean drop-in:

```python
# At startup:
agent = CursorAgentSession(workspace=Path(AGENT_WORKSPACE))

# On WAKE_DETECTED (alongside opening Deepgram WebSocket):
agent.prepare()

# After VAD_STOPPED + transcript assembled:
for text_chunk in agent.run(transcript):
    tts_queue.put(text_chunk)

# On shutdown:
agent.close()
```

`cognitive_loop(audio_bytes, dg_client)` is retired and replaced by direct `agent.run(transcript)` iteration in the WAKE/VAD event handler. The `transcribe()`, `run_claude()`, and `stub_claude()` functions in `master.py` are removed.

---

## Forward Interchangeability

Other backends implement `AgentSession` with the same interface:

| Class | Backend | Notes |
|-------|---------|-------|
| `CursorAgentSession` | Cursor CLI (`~/.local/bin/agent`) | Current implementation |
| `ClaudeAgentSession` | Claude CLI (`claude -p --output-format stream-json`) | Alternative; subject to token quota |
| _(future)_ | Direct Anthropic API | No subprocess; async iterator |
| _(future)_ | Alternative model CLI | Same base, different binary |

`master.py` only imports and instantiates the concrete class. Swapping backends is a one-line change in the master configuration block.

**Deployment note:** On Pi, `AGENT_BIN` may point at a **wrapper** that supervises the real Cursor CLI; the argv contract in §Subprocess Invocation is unchanged. See `cursor_agent_wrapper_spec.md`.
