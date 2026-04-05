
## Agent subprocess shutdown — weaknesses and options

**Observation:** The Cursor `agent` process (or its children) can **outlive** `master.py` or remain running when the operator expects a clean shutdown.

Below: how the current code behaves, why gaps exist, and options to harden guarantees (no code changes in this document — pick options in implementation).

### Current behaviour (reference)

- [agent_session.py](agent_session.py) `CursorAgentSession.close()` — if `Popen.poll()` is still `None`, calls `terminate()`, `wait(timeout=3)`, then `kill()` on timeout. Clears `_process`.
- [master.py](master.py) `master_loop` — `finally:` calls `agent.close()` then `tts.close()`. Runs on normal return (`SHUTDOWN_COMMENCED`), exceptions from the pipe loop, and (for `KeyboardInterrupt` raised inside the loop) after the `finally` runs before `main()` handles it.
- Subprocess is started with **`start_new_session=True`** so the spawned process is a new session leader (see `Popen` in `prepare()`).

### Weaknesses

1. **Single-PID signal** — `terminate()` / `kill()` target only the **direct child** of `Popen`. The Cursor CLI often spawns **workers** (e.g. Node, helper processes). Those may **not** receive the signal and can keep running as orphans or background jobs.

2. **`sudo` indirection** — With `AGENT_USER` set, the direct child is **`sudo`**, not `agent`. Signal delivery and exit semantics depend on `sudo` forwarding teardown to the real agent child. The agent may survive if `sudo` exits first or if the child is in a different process group.

3. **No process-group kill** — Even with `start_new_session=True`, the session leader is the **first** process (`sudo` or `agent`). Descendants are not guaranteed to be in the **same process group** as that leader. **`os.killpg(pid, SIGTERM)`** on the leader is not used today; it would help only when the whole tree shares one group (verify with `ps -o pid,pgid,sid` on the Pi).

4. **Early consumer stop (`GeneratorExit`)** — `master.py` does `tts.play(agent.run(transcript))`. If `play()` stops iterating early (error, hang recovery, future cancellation), Python closes the generator with **`GeneratorExit`**. `run()` has **no `try`/`finally`** that always waits or terminates the subprocess on generator exit. The agent can **keep running** with stdin already closed, and `_process` may still point at a live PID while the generator frame is torn down — **inconsistent state** for the next `prepare()` / `run()`.

5. **`stderr` PIPE** — `stderr=subprocess.PIPE` without a reader thread can fill the pipe if the agent is verbose under load; that can contribute to **stalls** (less often “persisting after exit,” but worth draining on shutdown).

6. **Hard kills** — `kill -9` on master or `os._exit()` skips Python `finally` blocks; no user-space guarantee (acceptable edge case).

### Options to strengthen shutdown (ordered roughly by invasiveness)

| Option | Idea | Pros | Cons |
|--------|------|------|------|
| **A. `try`/`finally` in `run()`** | Wrap the body of `run()` so **any** exit (normal, exception, `GeneratorExit`) runs cleanup: terminate/kill subprocess, drain or close pipes, set `_process = None`. | Fixes early-stop / TTS abort without relying on `close()`. Single place for “run owns the process until finished.” | Must avoid double-`wait` if `close()` is also called; idempotent cleanup helper. |
| **B. Process-group terminate** | After spawn, record leader PID; on shutdown use **`os.killpg(pid, SIGTERM)`** (with fallback to `terminate()` if `killpg` fails / wrong platform). | One signal can reach the whole CLI tree **when** the tree shares the group. | Wrong if children use their own group; **`sudo`** may break the model — test on Pi. |
| **C. Recursive child kill** | Use **`/proc/<pid>/task/*/children`** (Linux) or a small **psutil** dependency to find descendants and signal them. | Precise when PGID is not enough. | More code; psutil is an extra dep unless hand-rolled. |
| **D. Wrapper / `prctl`** | Linux: wrapper sets **`PR_SET_PDEATHSIG`** so agent tree dies when parent dies; or a shell wrapper that traps and kills children. | Strong “parent died” semantics. | Extra binary/script; must integrate with `AGENT_USER` path. |
| **E. Dedicated shutdown from `main()`** | Register **`signal.signal`** for SIGINT/SIGTERM to call `agent.close()` before re-raising, in addition to `master_loop` `finally`. | Belt-and-suspenders if something blocks `finally` ordering. | Duplication unless factored; still does not fix orphan children without A–D. |
| **F. Drain `stderr` on close** | Background thread or `communicate()`-style drain after terminate. | Avoids deadlock on full pipe. | Small complexity. |

**Practical recommendation:** Implement **A** first (guaranteed cleanup on every `run()` exit path). Add **B** or **C** if orphan PIDs are still visible after Pi testing. Use **F** if stderr-driven stalls appear in logs.

### Verification on the Pi

After any change:

1. `pgrep -af agent` (or `~/.local/bin/agent`) before and after `master.py` exits.
2. Same check after **Ctrl+C during TTS** (early generator stop).
3. With **`AGENT_USER`**, confirm no `sudo`/`agent` pair left behind.

---

## Related memory

- [../../memory/shutdown_and_buffer_patterns.md](../../memory/shutdown_and_buffer_patterns.md) — recorder/master shutdown protocol.
- [../archive/2026-04-04_wrapped_cursor_agent_context.md](../archive/2026-04-04_wrapped_cursor_agent_context.md) — Cursor CLI stream-json schema.
