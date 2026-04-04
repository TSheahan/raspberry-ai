# Cursor Agent Integration — Context and Findings

**Date:** 2026-04-04
**Scope:** Cursor CLI smoke test results informing EU-6 (agent layer) design for `master.py`

---

## 1. The `agent` Binary

`~/.local/bin/agent` is the official Cursor CLI, installed via:

```bash
curl https://cursor.com/install -fsS | bash
```

It runs headlessly with `-p` (print mode), accepts a prompt on stdin, and streams structured JSON to stdout. Full help reference in §A below.

---

## 2. Invocation Pattern

```python
subprocess.Popen(
    ["agent", "-p",
     "--output-format", "stream-json",
     "--stream-partial-output",
     "--force", "--yolo", "--trust",
     "--workspace", "/path/to/workspace",
     "--model", "claude-4.6-sonnet-medium",
     "--resume", session_id,   # omit on first turn
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)
```

The process is spawned immediately on WAKE_DETECTED and sits idle waiting for stdin. When the STT transcript is ready, it is written to stdin and stdin is closed. This hides the agent startup latency behind the Deepgram streaming window.

---

## 3. Output Event Schema (`stream-json + --stream-partial-output`)

Each stdout line is a JSON object. The relevant event types:

| `type` | Has `timestamp_ms` | Purpose |
|--------|--------------------|---------|
| `system` / `subtype: init` | — | Session open: confirms `session_id`, `model`, `cwd` |
| `user` | — | Echo of the prompt sent via stdin |
| `assistant` | **yes** | Incremental token delta — accumulate for streaming TTS |
| `assistant` | no | Final accumulated message — ignore for accumulation |
| `result` / `subtype: success` | — | Authoritative final text in `result` field; also carries `session_id`, `usage` |

**Accumulation rule:** only accumulate `type == "assistant"` objects that carry `timestamp_ms`. The trailing non-timestamped assistant object is a duplicate of the accumulated content and should be skipped. Use `result.result` as the canonical final string.

**Session continuity:** `session_id` is stable across the turn and is emitted on every event. Capture it from any event; pass as `--resume <session_id>` on the next spawn to continue the conversation in context.

---

## 4. Workspace Context

The agent reads the workspace at startup — the first turn in the sample run produced `"I've seen AGENTS.md."`, confirming that `AGENTS.md` and project rules are loaded before the first response. The production workspace will be the `personal` project checkout on the Pi (`~/personal` or equivalent), which carries structured agentic memory, managed memory files, and tooling. This is the intended source of the agent's identity and context for the voice assistant.

---

## 5. TTS Word-Boundary Observation

When feeding incremental tokens to a TTS engine, do not dispatch the last whitespace-delimited fragment of a delta until the next delta arrives (or the stream ends). The next delta may begin with a word continuation (e.g., delta N ends `"contin"`, delta N+1 begins `"uation"`). Only dispatch a word once the following delta confirms it is complete, or the `result` event signals the stream has ended.

---

## 6. JSON Parsing Note

Do not implement custom JSON parsing for the stream-json output. Use a standard library (`json.loads` per line). If partial-line buffering becomes an issue, search for established third-party streaming JSON parsers before writing custom logic.

---

## 7. Core Shell Pinning Note

The agent subprocess spawned on WAKE_DETECTED will land on cores 2–3 naturally when the master runs on core 4 and the recorder child is pinned to core 1. Explicit `taskset` / `os.sched_setaffinity` pinning can be added later if isolation is needed; it is not required for the initial integration.

---

## Supplemental A — `agent` CLI Help

```
Usage: agent [options] [command] [prompt...]

Start the Cursor Agent

Arguments:
  prompt                       Initial prompt for the agent

Options:
  -v, --version                Output the version number
  --api-key <key>              API key for authentication (can also use CURSOR_API_KEY env var)
  -H, --header <header>        Add custom header to agent requests (format: 'Name: Value', can be used multiple times)
  -p, --print                  Print responses to console (for scripts or non-interactive use). Has access to all tools, including write and shell. (default: false)
  --output-format <format>     Output format (only works with --print): text | json | stream-json (default: "text")
  --stream-partial-output      Stream partial output as individual text deltas (only works with --print and stream-json format) (default: false)
  -c, --cloud                  Start in cloud mode (open composer picker on launch) (default: false)
  --mode <mode>                Start in the given execution mode. plan: read-only/planning (analyze, propose plans, no edits). ask: Q&A style for explanations and questions (read-only). (choices: "plan", "ask")
  --plan                       Start in plan mode (shorthand for --mode=plan). Ignored if --cloud is passed. (default: false)
  --resume [chatId]            Select a session to resume (default: false)
  --continue                   Continue previous session (default: false)
  --model <model>              Model to use (e.g., gpt-5, sonnet-4, sonnet-4-thinking)
  --list-models                List available models and exit (default: false)
  -f, --force                  Force allow commands unless explicitly denied (default: false)
  --yolo                       Alias for --force (Run Everything) (default: false)
  --sandbox <mode>             Explicitly enable or disable sandbox mode (overrides config) (choices: "enabled", "disabled")
  --approve-mcps               Automatically approve all MCP servers (default: false)
  --trust                      Trust the current workspace without prompting (only works with --print/headless mode) (default: false)
  --workspace <path>           Workspace directory to use (defaults to current working directory)
  -w, --worktree [name]        Start in an isolated git worktree at ~/.cursor/worktrees/<reponame>/<name>. If omitted, a name is generated.
  --worktree-base <branch>     Branch or ref to base the new worktree on (default: current HEAD)
  --skip-worktree-setup        Skip running worktree setup scripts from .cursor/worktrees.json (default: false)
  -h, --help                   Display help for command

Commands:
  install-shell-integration    Install shell integration to ~/.zshrc
  uninstall-shell-integration  Remove shell integration from ~/.zshrc
  login                        Authenticate with Cursor. Set NO_OPEN_BROWSER to disable browser opening.
  logout                       Sign out and clear stored authentication
  mcp                          Manage MCP servers
  status|whoami                View authentication status
  models                       List available models for this account
  about                        Display version, system, and account information
  update                       Update Cursor Agent to the latest version
  create-chat                  Create a new empty chat and return its ID
  generate-rule|rule           Generate a new Cursor rule with interactive prompts
  agent [prompt...]            Start the Cursor Agent
  ls                           Resume a chat session
  resume                       Resume the latest chat session
  help [command]               Display help for command
```

---

## Supplemental B — Sample Run Output

Two-turn session on Pi. Model: `claude-4.6-sonnet-medium` (Sonnet 4.6 1M context). Workspace: `~/test-project-1` (contains `AGENTS.md`).

```
agent@morpheus:~ $ python pipe_wrapper_v4.py
✅ Plan B v4 smoke test (fixed streaming) ready — workspace: /home/agent/test-project-1
   Model: claude-4.6-sonnet-medium
   Output: stream-json + --stream-partial-output
   Accumulation fix: only timestamped assistant deltas are added

You (multi-line, end with blank line):
Hello!
→ Pre-spawning streaming agent (fresh chat)
   Agent process spawned and waiting for stdin (latency hidden)...

→ Feeding full turn to already-running streaming agent...
STREAM → {"type":"system","subtype":"init","apiKeySource":"login","cwd":"/home/agent/test-project-1","session_id":"24e93dd3-e86a-4294-a113-847982438164","model":"Sonnet 4.6 1M","permissionMode":"default"}
STREAM → {"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello!"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164"}
STREAM → {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164","timestamp_ms":1775269215944}
STREAM → {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"'ve seen AGENTS.md.\n\nHello! How can I help you today?"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164","timestamp_ms":1775269216227}
STREAM → {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I've seen AGENTS.md.\n\nHello! How can I help you today?"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164"}
STREAM → {"type":"result","subtype":"success","duration_ms":7068,"is_error":false,"result":"I've seen AGENTS.md.\n\nHello! How can I help you today?","session_id":"24e93dd3-e86a-4294-a113-847982438164","usage":{"inputTokens":3,"outputTokens":21,"cacheReadTokens":12225,"cacheWriteTokens":1174}}
   ✅ Captured session_id for next turn: 24e93dd3...

=== FULL AGENT REPLY (accumulated from streaming tokens) ===

I've seen AGENTS.md.

Hello! How can I help you today?

================================================================================

You (multi-line, end with blank line):
If I had one cat, and I got another cat, how many cats would I have?
→ Pre-spawning streaming agent (resuming chat 24e93dd3...)
   Agent process spawned and waiting for stdin (latency hidden)...

→ Feeding full turn to already-running streaming agent...
STREAM → {"type":"system","subtype":"init","apiKeySource":"login","cwd":"/home/agent/test-project-1","session_id":"24e93dd3-e86a-4294-a113-847982438164","model":"Sonnet 4.6 1M","permissionMode":"default"}
STREAM → {"type":"user",...}
STREAM → {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Two"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164","timestamp_ms":1775269273752}
STREAM → {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":" cats!"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164","timestamp_ms":1775269273838}
STREAM → {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Two cats!"}]},"session_id":"24e93dd3-e86a-4294-a113-847982438164"}
STREAM → {"type":"result","subtype":"success","duration_ms":3007,"is_error":false,"result":"Two cats!","session_id":"24e93dd3-e86a-4294-a113-847982438164","usage":{"inputTokens":3,"outputTokens":6,"cacheReadTokens":13519,"cacheWriteTokens":1229}}
   ✅ Captured session_id for next turn: 24e93dd3...

=== FULL AGENT REPLY (accumulated from streaming tokens) ===

Two cats!

================================================================================

You (multi-line, end with blank line):
^C

👋 Smoke test interrupted.
```
