# Agent Session Patterns

**Date:** 2026-04-04
**Domain:** Master process — agent subprocess lifecycle, streaming output, session continuity
**Related files:** `forked_assistant/src/agent_session.py`, `forked_assistant/spec/agent_session_spec.md`, `forked_assistant/archive/2026-04-04_wrapped_cursor_agent_context.md`

---

## Pre-Spawn Pattern

The agent process is spawned the moment WAKE_DETECTED is received — before the transcript is available. The process starts up, loads the workspace (AGENTS.md, project rules, memory files), and waits with stdin open. By the time VAD_STOPPED fires and the Deepgram streaming transcript is assembled, the process is already initialised.

```
WAKE_DETECTED
    │
    ├── open Deepgram live WebSocket  (EU-5)
    └── agent.prepare()               (EU-6: spawns agent subprocess, stdin open)

    ... recorder child captures audio, ring buffer fills, Deepgram transcribes ...

VAD_STOPPED
    │
    ├── send_finalize() → collect transcript
    └── agent.run(transcript) → yields text deltas → TTS pipeline
```

This hides the agent startup cost (~3–7s on first turn, ~3s on resumed turns) behind the Deepgram streaming window. In practice, by the time the user finishes speaking and VAD_STOPPED fires, the agent has been running for the duration of the utterance.

**Implementation:** `prepare()` must be idempotent if called more than once per turn (guard against double-spawn). If `prepare()` was not called before `run()`, `run()` should call it internally.

---

## Session Continuity Model

Each successful `run()` call updates `last_turn_time` (monotonic clock). On the next `prepare()` call:

- If `session_id` is set **and** `time.monotonic() - last_turn_time < resume_window_secs`: pass `--resume session_id` to the agent subprocess → conversation context is retained
- Otherwise: spawn a fresh session → no prior context

```
last_turn_time = 0 (no prior turn)
    → always fresh

last_turn_time set, elapsed < resume_window_secs
    → --resume session_id

last_turn_time set, elapsed >= resume_window_secs
    → fresh (session_id cleared)
```

**Default window:** 300 seconds (5 minutes). Configurable via `AGENT_RESUME_WINDOW_SECS` env var or constructor argument. Setting to 0 disables resume entirely.

**Rationale:** Conversational voice use expects continuity across rapid back-and-forth. After a long pause (e.g., user leaves and returns), starting fresh prevents stale context from contaminating responses. The window is the single control knob — no explicit "end session" command is needed.

**Session ID source:** Captured from any event in the stream-json output that carries `session_id` (all event types include it). It is stable throughout a turn and is stored after the first event arrives, before the response completes.

---

## stream-json Event Schema (Cursor CLI)

The Cursor CLI with `--output-format stream-json --stream-partial-output` emits one JSON object per stdout line. Three event types are consumed:

### `system` / `init`

```json
{"type": "system", "subtype": "init", "session_id": "...", "model": "Sonnet 4.6 1M", "cwd": "/path/to/workspace", ...}
```

Confirms session open, workspace, and model. Carries `session_id` — capture immediately.

### `assistant` delta (has `timestamp_ms`)

```json
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Two"}]}, "session_id": "...", "timestamp_ms": 1775269273752}
```

**Presence of `timestamp_ms` = incremental streaming token.** Accumulate for TTS and for full-text logging.

### `assistant` final (no `timestamp_ms`)

```json
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Two cats!"}]}, "session_id": "..."}
```

**Absence of `timestamp_ms` = final accumulated duplicate.** Skip for accumulation — this is a replay of all prior deltas concatenated. Using `result` instead is cleaner.

### `result`

```json
{"type": "result", "subtype": "success", "result": "Two cats!", "session_id": "...", "is_error": false, "duration_ms": 3007, "usage": {"inputTokens": 3, "outputTokens": 6, "cacheReadTokens": 13519, "cacheWriteTokens": 1229}}
```

Authoritative final text in `result` field. `is_error: false` confirms clean exit. `usage` carries token counts — `cacheReadTokens` will be large on resumed sessions (workspace context served from cache).

**Accumulation rule:** yield from `assistant` events with `timestamp_ms`; use `result.result` as the canonical final string for logging and error recovery.

---

## TTS Word-Boundary Buffer

Incremental token deltas may split mid-word across events:

```
delta 1: "contin"
delta 2: "uation of the"
delta 3: " story"
```

Feeding "contin" to a TTS engine produces a mispronunciation or abrupt cut. The buffer rule:

1. On each delta, append to the buffer.
2. Find the last whitespace position in the buffer.
3. Everything up to (and including) the last whitespace is safe to yield — it cannot be extended by the next delta.
4. Retain the tail (after last whitespace) in the buffer.
5. On stream end, flush the entire remaining buffer.

```python
buffer = ""
for delta_text in raw_deltas:
    buffer += delta_text
    last_ws = buffer.rfind(" ")
    if last_ws >= 0:
        yield buffer[:last_ws + 1]
        buffer = buffer[last_ws + 1:]
if buffer:
    yield buffer  # flush tail
```

**Note:** Newlines (`\n`) are also word boundaries for TTS purposes. The implementation should treat `\n` equivalently to space when finding the flush boundary.

---

## JSON Parsing — No External Library

No suitable PyPI library exists for parsing the Cursor CLI stream-json format programmatically:

- `cursor-cli` (PyPI): unknown publisher, 0 stars, terminal formatter only — not a pipeline parser
- `claude-agent-sdk` (Anthropic official): trusted source but manages its own subprocess; no `parse_line()` API; 58 MB bundled binary; async-only; schema differs from Cursor CLI's `timestamp_ms` pattern

**Decision:** `json.loads(line)` per line with local TypedDict definitions. Zero new dependencies. TypedDicts serve as in-code schema documentation and enable static analysis.

```python
from typing import TypedDict, Literal, NotRequired

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
```

See `forked_assistant/src/agent_session.py` for the full implementation.
