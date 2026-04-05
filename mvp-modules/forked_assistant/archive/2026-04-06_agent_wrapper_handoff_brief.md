# Agent wrapper — handoff brief for a fresh design session

**Location:** `mvp-modules/forked_assistant/archive/2026-04-06_agent_wrapper_handoff_brief.md` (date-prefixed retention; formerly under `profiling-pi/`).

**Purpose:** This file is the **only** reliable briefing for designing the Cursor CLI wrapper and related provisioning. A prior design pass was **aborted**; repository state may have diverged. **Re-read the cited paths on disk** before trusting any “tainted” notes below.

**This document:** instructs a **new** session to produce the **design product** (markdown spec). **No code implementation** is required unless the new session explicitly expands scope.

---

## Authoritative requirements (from product owner — trust these)

1. **Design product is markdown** — the deliverable is specification text, not necessarily code in the first pass (unless the session chooses to implement).

2. **Termination handler (sudo escape hatch)** — document as a **fallback** only, for **possible later implementation**. Do **not** rely on it as the primary teardown story. Primary story remains an **agent-side wrapper** (see prior preference: wrapper over broadened sudo for kill/pkill).

3. **Wrapper script** — interposed on **`agent` execution** invoked from the **voice app** (`master.py` / `CursorAgentSession`). `voice` continues to use the existing narrow `sudo -u agent` pattern; the sudoers target becomes the wrapper (or wrapper + fixed real binary path — to be decided in the design).

4. **Logging** — assess (in the design) directing **wrapper logs and errors** to **`/var/log/agent-wrapper.log`** for **start / clobber / deposition** diagnostics. Call out: permissions (`voice` vs `agent` vs logrotate), append vs truncate, and whether stderr of the real CLI should also be tee’d or left on PIPE for Python.

5. **Version control** — the wrapper **must be tracked in this repo** and **re-installed when it changes** (deterministic sync from checkout to runtime location).

6. **New repo root folder** — add a **dedicated top-level directory** for **`voice` user artifacts** (sparse; the wrapper is the **first** member). Name is a design choice (e.g. `voice-artifacts/`); document layout and naming in the spec.

7. **`profiling-pi` install-time responsibility** — extend profiling instructions so an agent (or operator) performing install/provision must:
   - **Install an update hook** on the **local checkout** of `raspberry-ai` (e.g. `post-merge` / `post-checkout` git hook, or another hook pattern — **design** the exact mechanism) so that when the repo updates, the voice-runtime copy of the wrapper is refreshed.
   - **Profile a sudo permission** in the same **narrow** style as the existing `voice ALL=(agent) NOPASSWD: …` entry — but for whatever the **update hook** needs to **deposit** the wrapper into **`/home/voice/`** (or the chosen install path). Document the exact sudoers line(s) and rationale.
   - **Run the first execution** of the wrapper install (bootstrap) as part of profiling so the Pi reaches target state in one documented flow.

---

## Tainted context (stale view — verify before use)

The following reflected **pre-pull** understanding; **lines and behaviour may differ** now.

| Topic | Tainted note | Action for fresh session |
|-------|----------------|---------------------------|
| Subprocess teardown | `CursorAgentSession.close()` only signals the **direct** `Popen` child; Cursor CLI may spawn children as **`agent`** that **`voice` cannot signal**. | Re-read `mvp-modules/forked_assistant/src/agent_session.py` and `master.py`. |
| Early generator exit | `run()` may lack `try`/`finally` if consumer stops iterating early (`tts.play(agent.run(...))`). | Re-read `agent_session.py` generator body. |
| Shutdown write-up | Teardown analysis: [2026-04-06_agent_process_shutdown.md](../../archive/2026-04-06_agent_process_shutdown.md). `src/AGENTS.md` is a **file index** only. | Re-read if present; update if design supersedes. |
| Sudoers today | `voice ALL=(agent) NOPASSWD: /home/agent/.local/bin/agent` and `.env` keys `AGENT_USER`, `AGENT_BIN`, `AGENT_WORKSPACE`. | Re-read `profiling-pi/agent-user-setup.md` and `.env` examples. |
| Linux users | Voice assistant OS user is **`voice`**; Cursor subprocess target is **`agent`**. | Confirm in current `agent-user-setup.md` / `voice-user-setup.md`. |
| Design preference | **Prefer agent-side wrapper** over expanding sudo so `voice` can kill arbitrary `agent` PIDs; termination handler = optional fallback. | Still valid as **product direction**; confirm with owner if ambiguous. |

---

## Suggested deliverables for the fresh session

1. **Design markdown** (path TBD in spec — e.g. under `mvp-modules/forked_assistant/spec/` or next to the wrapper in the new voice-artifacts tree): wrapper **behaviour**, **signal/propagate** model, **exec** vs **supervise** child, **logging** policy, **failure** modes, interaction with **`AGENT_BIN`** / `CursorAgentSession` argv assembly.

2. **Repository layout** — document the new **root folder**, wrapper filename(s), and **install target** under `/home/voice/…`.

3. **`profiling-pi` delta spec** — new or extended markdown: Target state / Instructions / Verify for hook installation, sudoers for hook, first-run install; align with `profiling-pi/AGENTS.md` provisioning order table.

4. **Fallback appendix** — short subsection: **termination-handler** sudo escape hatch — scope, risks, when to implement later.

---

## Files to read first (ground truth = current tree)

- `mvp-modules/forked_assistant/src/agent_session.py`
- `mvp-modules/forked_assistant/src/master.py`
- `profiling-pi/agent-user-setup.md`
- `profiling-pi/voice-user-setup.md` (home layout, repo path)
- `profiling-pi/AGENTS.md`

---

## Explicit non-goals for the handoff brief itself

- This file does **not** define the final wrapper implementation.
- It does **not** assume the aborted session’s partial plans survived the pull.

When the fresh session completes, it may add a single line here pointing to the **canonical design doc** path for traceability.
