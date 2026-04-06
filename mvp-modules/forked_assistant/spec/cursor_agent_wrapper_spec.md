# Cursor agent wrapper — design specification

**Date:** 2026-04-06  
**Status:** Implemented (repo: `agent-artifacts/cursor-agent-wrapper`; install: `profiling-pi/cursor-agent-wrapper-provisioning.md`)  
**Related:** [2026-04-04_wrapped_cursor_agent_context.md](../archive/2026-04-04_wrapped_cursor_agent_context.md) (CLI flags, stream-json schema), [agent_session_spec.md](./agent_session_spec.md) (`CursorAgentSession`), [2026-04-06_agent_process_shutdown.md](../../archive/2026-04-06_agent_process_shutdown.md) (teardown gaps)

---

## 1. Purpose

Interpose a **small, version-controlled program** on the path from `voice_assistant.py` / `CursorAgentSession` to the real Cursor CLI binary. Today `voice` runs (conceptually):

`sudo -u agent -H -- <AGENT_BIN> <cursor flags…>`

with `AGENT_BIN` pointing at `/home/agent/.local/bin/agent`. The wrapper becomes `AGENT_BIN`: same argv contract as the real CLI (so `CursorAgentSession` unchanged except env/path), while adding **deterministic install**, **diagnostic logging**, and a **supervising parent** that can forward teardown signals to the CLI process group (§5).

---

## 2. Problems the wrapper addresses

| Issue | How the wrapper helps |
|--------|------------------------|
| **Opaque failures** at spawn (wrong cwd, bad install, permission) | Append-only log of **start**, **which real binary**, **argv summary** (see §6 — argv is flags/paths only) |
| **Provisioning drift** | Repo-owned script + install step + git hook → known runtime path under `/home/agent/artifacts/` (§4) |
| **Teardown / orphan workers** | **Supervising parent** (§5b): forwards SIGTERM/SIGINT to child process group; complements Python **`run()` `finally`** / `close()` — see §11 |
| **Narrow sudo** | Sudoers grants **one** fixed path; that path is the wrapper, not “any `agent`” |

Non-goals for v1: changing stream-json parsing, model flags, or `AgentSession` API.

---

## 3. Repository layout (new root directory)

Add a **sparse** top-level directory at the repo root (sibling to `mvp-modules/`, `profiling-pi/`):

| Path | Role |
|------|------|
| `agent-artifacts/cursor-agent-wrapper` | Supervising launcher (implements §5b); installed to `/home/agent/artifacts/` |
| `agent-artifacts/README` | Optional one-line pointer to this spec (avoid duplicating normative text) |

**Naming:** The `raspberry-ai` checkout is **owned and operated by the `voice` user**, but this tree is reserved for payloads whose **runtime alignment is the `agent` user** — executed under `sudo -u agent`, installed under `/home/agent/…`, and auditable as the agent-side surface. The name **`agent-artifacts/`** makes that link explicit (not “voice app assets” in general).

**Version control:** Every change to the wrapper file must be committed; Pi **re-install or sync** is part of operator/agent workflow (see §8).

---

## 4. Runtime install target on the Pi — **§4a only (locked)**

**There is no in-checkout `AGENT_BIN` mode.** Production and profiling both use an **agent-home copy** only.

- **Install path (canonical):** `/home/agent/artifacts/cursor-agent-wrapper`
- **Owner:** `agent:agent`, mode `0755`
- **Parent directory:** `/home/agent/artifacts/` created by the installer if missing, owned `agent:agent`, mode `0755`
- **Rationale:** `agent` never needs to traverse `/home/voice/…`; `voice` home stays private. Directory name matches the repo tree concept (`agent-artifacts` → `~/artifacts/` under `agent`).
- **`AGENT_BIN` in `/home/voice/.env`:** `/home/agent/artifacts/cursor-agent-wrapper`
- **Real CLI:** `/home/agent/.local/bin/agent` — wrapper uses a **fixed default** or `CURSOR_AGENT_REAL_BIN` / `AGENT_REAL_BIN` env (provisioning sets how `agent`’s environment sees this).

---

## 5. Wrapper behaviour: supervise vs thin `exec`

### 5b. Mode B — Supervising parent **(selected)**

1. Wrapper process stays resident (effective user **`agent`** after `sudo -u agent`).
2. Spawns the real Cursor CLI as a **child** in a **new session / process group** (`setsid`) so **`kill -TERM -- -$child`** on the child’s PGID targets the CLI tree. **Stdin preservation:** bash `&` redirects backgrounded jobs to `/dev/null`; the wrapper saves stdin to fd 3 before backgrounding and re-attaches it to the `setsid` child (`exec 3<&0; setsid -- "$REAL_BIN" "$@" <&3 &`). This keeps the prompt pipe from `CursorAgentSession` connected while still giving the child its own process group.
3. On **SIGTERM** / **SIGINT**, forward to the child process group; reap child; exit with the child’s exit status (or a defined mapping on signal-induced death).
4. **stderr:** Child’s stderr remains the **same fd** as inherited from the parent chain so Python’s **`stderr=subprocess.PIPE`** in `CursorAgentSession` still receives CLI diagnostics (no duplicate tee to files unless explicitly added later).

**Validation (2026-04-06):** Confirmed on Pi with `AGENT_USER` + `sudo`. `ps -o pid,pgid,sid` shows child PGID = child PID, differs from wrapper PGID. Smoke test passes end-to-end through wrapper.

**Product direction:** Prefer correctness from **wrapper + `CursorAgentSession`**, not from broad sudo for arbitrary `kill`.

### 5a. Mode A — Thin `exec` shim **(not selected)**

`exec` the real binary after a one-line log so the wrapper PID becomes the CLI. **Not chosen:** user selected supervise mode for clearer process-group teardown. Kept in this spec only as a documented alternative if a future deployment needs minimal moving parts and accepts weaker tree signals.

---

## 6. Logging policy

**File:** `/var/log/agent-wrapper.log` (append-only).

| Topic | Recommendation |
|--------|----------------|
| **Permissions** | File `agent:agent`, mode **`0640`** so only **agent** (and root) can read/write. Directory `/var/log` remains root-owned as usual. |
| **Argv vs secrets** | The **user utterance is not on argv** — `CursorAgentSession` passes the transcript on **stdin**; argv is Cursor flags and paths only. **Risk from logging argv is therefore low** (no interactive paste into shell args). Still **do not** log environment blobs that may hold API keys. **Caller annotation:** documented on `CursorAgentSession` and in `agent_session_spec.md` so future changes do not move sensitive text onto argv. |
| **Rotation** | `logrotate` drop-in — see **profiling gap** in `profiling-pi/cursor-agent-wrapper-provisioning.md` (wanted vs current). |
| **Content** | ISO-ish timestamp, events: `start`, `spawn_real`, `signal_forward`, `fatal` (missing binary, spawn failure) — **no** API keys; cap argv string length in logs if desired |
| **Python stderr** | Unchanged: `CursorAgentSession` reads CLI stderr via **`stderr=subprocess.PIPE`**; supervising wrapper forwards child stderr to that pipe |

**Truncate vs append:** Always **append**; never truncate per invocation.

---

## 7. Sudoers model — **`voice` deposits to `agent`**

Two distinct **voice → agent** touch points:

| Touch point | Mechanism | Purpose |
|-------------|-----------|---------|
| **Runtime invoke** | `voice ALL=(agent) NOPASSWD: /home/agent/artifacts/cursor-agent-wrapper` | `voice_assistant.py` runs `sudo -u agent -H -- <AGENT_BIN> …`; **only** the wrapper path is allowed — not the raw CLI if policy is wrapper-only. |
| **Deposit / sync** | `voice ALL=(ALL) NOPASSWD: <fixed installer script path>` | **`voice`** runs the installer (after `git pull`) to **copy** `agent-artifacts/cursor-agent-wrapper` from the **voice checkout** into **`/home/agent/artifacts/`**, `chown agent:agent`. This is how repo updates reach **agent-owned** bytes without giving `voice` write access to `/home/agent` except through that one script. |

**Today (pre-wrapper):**  
`voice ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent`

**After wrapper:** Replace with wrapper path as above; remove standalone NOPASSWD to raw CLI if policy is “wrapper only.”

**`sudo -u agent -H --`:** Unchanged in Python; first argv after `--` is the wrapper at `/home/agent/artifacts/cursor-agent-wrapper`.

---

## 8. Git update hook & bootstrap (profiling-pi)

Normative step-by-step lives in **`profiling-pi/cursor-agent-wrapper-provisioning.md`**. Summary:

1. **Hook:** `post-merge` / `post-checkout` invoke the **fixed** installer → copies into **`/home/agent/artifacts/`** (§4).
2. **Sudo for install:** Narrow NOPASSWD for that installer script only.
3. **Bootstrap:** First provision run installs wrapper + log file permissions (see provisioning **wanted vs current** for logging).

**Determinism:** Git change to wrapper → hook or manual installer → updated file on **`agent`** home.

---

## 9. Interaction with `CursorAgentSession` / `AGENT_BIN`

- `voice_assistant.py` continues: `CursorAgentSession(..., agent_bin=_AGENT_BIN)` with `_AGENT_BIN` from env.
- `.env` sets `AGENT_BIN=/home/agent/artifacts/cursor-agent-wrapper`.
- Wrapper resolves the real binary per §4.
- No change to flags: `-p`, `stream-json`, `--stream-partial-output`, `--force`, `--yolo`, `--trust`, `--workspace`, `--model`, optional `--resume`.

---

## 10. Failure modes

| Failure | Behaviour |
|---------|-----------|
| Real binary missing | Log `fatal`, exit non-zero **before** spawning child |
| Log unwritable | **stderr only** for log lines; continue spawn (documented in `profiling-pi/cursor-agent-wrapper-provisioning.md`) |
| Sudoers points at old path | Operator fix; provisioning verify |
| Wrapper updated but hook not run | Stale bytes under `/home/agent/artifacts/`; verify compares checksum or mtime |

---

## 11. Python-side teardown (complements wrapper)

Supervise mode improves group signal delivery, but **`CursorAgentSession.run()`** must still use **`try`/`finally`** so **early generator exit** (`GeneratorExit`) always terminates the **supervising** process and drains pipes — see [2026-04-06_agent_process_shutdown.md](../../archive/2026-04-06_agent_process_shutdown.md). If orphan PIDs remain after Pi testing, consider **`killpg`** from Python on the **direct child’s PGID** as an additional layer.

---

## Appendix A — Orphan reaping (implemented 2026-04-06)

**Supersedes:** Previous “escape hatch” stub. Orphan `worker-server` processes were confirmed in production — the Cursor CLI spawns a `worker-server` child ~15s in, then the leader exits cleanly at ~20s, leaving the worker reparented to init at ~140MB RSS each. Four agent turns would OOM a 1GB Pi.

**Implementation (in wrapper):**

1. **`pgid_snapshot` fix:** `ps -g` selects by *session*, not process group; replaced with `pgrep --pgroup` → `ps -p` so orphans are visible after the session leader exits.
2. **`reap_orphans` function** runs after the leader exits and the exit event is logged:
   - **Settle** (`WRAPPER_ORPHAN_SETTLE`, default 3s) — lets worker-servers finish in-flight I/O.
   - **Snapshot** — if PGID is clean, log `status=clean` and return.
   - **SIGTERM** the whole PGID (`kill -TERM -- -$pgid`).
   - **Poll** up to `WRAPPER_ORPHAN_KILL_GRACE` (default 4s), checking each second.
   - **SIGKILL** any survivors; log final status.

**No extra sudo needed** — the wrapper runs as `agent` (same user as the orphans).

---

## Appendix B — Traceability

| Artifact | Role |
|----------|------|
| [2026-04-04_wrapped_cursor_agent_context.md](../archive/2026-04-04_wrapped_cursor_agent_context.md) | CLI invocation and stream-json rules |
| [agent_session_spec.md](./agent_session_spec.md) | `AgentSession` contract |
| [2026-04-06_agent_wrapper_handoff_brief.md](../archive/2026-04-06_agent_wrapper_handoff_brief.md) | Product requirements for this spec |

Canonical design: **this file** (`mvp-modules/forked_assistant/spec/cursor_agent_wrapper_spec.md`).

---

## Appendix C — Decision register

| # | Decision | Choice | Where enforced |
|---|----------|--------|----------------|
| D1 | Install layout | **§4a only** — `/home/agent/artifacts/cursor-agent-wrapper` | §4, provisioning installer |
| D2 | Wrapper mode | **§5b supervise**; §5a `exec` not selected | §5, implementation |
| D3 | Transcript transport | **stdin only** (not argv); caller docs | `agent_session_spec.md`, `CursorAgentSession` docstring |
| D4 | Log file mode | **`0640`**, `agent:agent` | §6, provisioning bootstrap |
| D5 | Log rotation | **Wanted**; profiling documents **gap** until logrotate drop-in added | Provisioning § “Logging” |
| D6 | `voice` → `agent` file sync | **Fixed installer** + sudoers; no broad write to `/home/agent` | §7, §8 |
| D7 | Sudo to run agent | **Wrapper path only** (wrapper-only policy) | §7, `agent-user-setup` migration note |
| D8 | Orphan reaping | **Closed** (2026-04-06) | Appendix A, wrapper `reap_orphans` |

**Coverage note:** Open items are **explicitly listed** (D5 rotation template). PGID validation under `sudo` is **closed** (2026-04-06 smoke test confirmed child PGID isolation). Everything else in this spec is **closed** for the first implementation pass.
