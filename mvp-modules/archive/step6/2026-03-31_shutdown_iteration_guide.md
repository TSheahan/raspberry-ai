# Step 6 Shutdown Iteration Guide — Piecemeal Isolation of Clean-Exit Failure

**Date:** 2026-03-31
**Status:** COMPLETE — v10 is the production baseline. Both bugs resolved (shutdown: Section 1a, buffer overflow: v10).
**Goal:** Identify the exact Pipecat component that prevents clean Ctrl+C exit, and either fix it or establish a production-viable workaround.

---

## 1. Purpose & Context

Step 6 of the voice pipeline PoC (`starting_brief.md`, build sequence steps 4–6) requires a pipeline that listens for a wake word, captures an utterance, transcribes it, and exits cleanly on Ctrl+C. The full Pipecat-based pipeline (`voice_pipeline_step6_pipecat_shutdown_v8.py`) does not exit cleanly — it hangs after Ctrl+C and must be forcibly terminated. A stripped-down diagnostic script (`voice_pipeline_step6_diagnostic_minimal_v4.py`) that uses direct PyAudio without Pipecat exits cleanly every time.

Eight one-shot attempts (v1–v8) to fix shutdown in the full pipeline have all failed. This document switches strategy: start from the working diagnostic baseline and incrementally re-introduce Pipecat components, testing shutdown at each step. The first increment that breaks shutdown isolates the cause.

**Referenced files:**
- `starting_brief.md` — Overall PoC brief and build sequence
- `step_6_progression_story.md` — History of attempts v1–v8
- `voice_pipeline_step6_diagnostic_minimal_v4.py` — Working baseline (clean exit)
- `voice_pipeline_step6_pipecat_shutdown_v8.py` — Latest failed attempt (hangs on exit)
- Pipecat source: `~/pipecat-agent/venv/lib/python3.13/site-packages/pipecat/pipeline/task.py`

---

## 1a. Root Cause — Missing `process_frame` Override (RESOLVED 2026-03-31)

**Finding:** `FrameProcessor.process_frame()` (the base class in Pipecat) does **not** push frames downstream. It only performs internal lifecycle bookkeeping — setting `__started` on `StartFrame`, setting `_cancelling` on `CancelFrame`, etc. It never calls `push_frame()`.

Every custom `FrameProcessor` subclass in a pipeline **must** override `process_frame` and call `await self.push_frame(frame, direction)` for all frame types it does not consume. Without this, frames (including `StartFrame` and `CancelFrame`) are silently swallowed at that processor.

**What happened in v8:** `DeepgramSTTProcessor` had no `process_frame` override. It relied on the base class default, which:
1. Received `StartFrame`, ran internal bookkeeping, but never pushed it onward → `PipelineTask` waited forever for `StartFrame` to reach the sink ("Waiting for StartFrame#0 to reach the end of the pipeline..." with no completion).
2. Received `CancelFrame` on Ctrl+C, ran internal bookkeeping, but never pushed it onward → `PipelineTask` waited for `CancelFrame` to reach the sink, hit the ~20s timeout, then required SIGTERM.

**The fix:** Every `FrameProcessor` subclass needs at minimum:
```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    await super().process_frame(frame, direction)
    await self.push_frame(frame, direction)
```

This is not documented in Pipecat. The base class API is misleading — `process_frame` reads like a template method that handles propagation, but it is actually a hook that requires the subclass to propagate explicitly.

**Isolation path:** v7 (with explicit `process_frame`) → PASS. v8b (identical to v7 minus `process_frame` on `DeepgramSTTProcessor`) → FAIL. Single-variable attribution confirmed.

---

## 1b. Change Audit — First Attempt → v8 Lineage

The first attempt (`voice_pipeline_step6_first_attempt.py`) was the original working script. Troubleshooting attempts v3–v7 accumulated jitter. v8 was a cleanup attempt that correctly stripped the jitter but accidentally also stripped `process_frame` from `DeepgramSTTProcessor`.

The v9 production script must apply **only the wanted changes** to the first attempt baseline, plus the root cause fix. Nothing from the troubleshooting jitter.

### Wanted changes (carry forward into v9)

| # | Change | Why | Where introduced |
|---|--------|-----|------------------|
| W1 | `dotenv` + `DEEPGRAM_API_KEY` check | Crash-early on missing key instead of opaque Deepgram error | v8 |
| W2 | `time.time()` instead of `asyncio.get_event_loop().time()` | Simpler, no event loop dependency for wall-clock debounce | v3 |
| W3 | `start_capture()` resets `utterance_buffer` | Prevents stale audio from prior wake detection leaking into new capture | v3 |
| W4 | Post-pipeline transcription (`_transcribe_sync` called after `runner.run()`) | First attempt tried to transcribe inline during `process_frame` when `not capturing` — racey, never triggered reliably. Post-pipeline call is deterministic. | v8 |
| W5 | Remove bare `import openwakeword` | Unused — `from openwakeword.model import Model` is sufficient | v8 |
| W6 | `process_frame` on **all three** custom processors, including `DeepgramSTTProcessor` | Required by Pipecat — base class does not push frames (Section 1a) | first_attempt (was correct originally) |
| W7 | Let `PipelineRunner` handle SIGINT (`handle_sigint` default `True`) | Pipecat's built-in handler queues a `CancelFrame` that propagates cleanly. Custom handlers were the troubleshooting jitter. | v8 |
| W8 | Plain `await runner.run(task)` — no try/except wrapper | `PipelineRunner` handles `CancelledError` internally. Wrapping it in `KeyboardInterrupt` or `CancelledError` catch was either redundant (v8) or harmful (first_attempt's `except KeyboardInterrupt` raced with Pipecat's handler). | v8 |
| W9 | No manual `transport.cleanup()` | `PipelineRunner` handles transport cleanup via `CancelFrame` propagation. Manual cleanup in finally blocks was double-cleanup from troubleshooting. | v8 |

### Unwanted changes (discard — troubleshooting jitter)

| # | Jitter | Present in | Why it's wrong |
|---|--------|-----------|----------------|
| J1 | Custom `signal.signal(SIGINT, handle_sigint)` | v3–v7 | Replaces Pipecat's working SIGINT handler. The whole premise ("Pipecat can't handle SIGINT") was wrong — the real bug was missing `process_frame`. |
| J2 | Global variables (`main_loop`, `pipeline_task`, `transport`, `stt_processor`) | v3–v7 | Only needed to support J1's custom signal handler. |
| J3 | `PipelineRunner(handle_sigint=False)` | v3–v7 | Disabled Pipecat's working handler to install J1. |
| J4 | `threading.Event` / `asyncio.Event` for shutdown coordination | v3, v7 | Only needed because J1 bypassed Pipecat's built-in shutdown flow. |
| J5 | Force-close PyAudio streams (`transport._audio_in_stream.stop_stream()`) | v3–v7 | Reaching into transport internals. Pipecat's `CancelFrame` does this correctly when it propagates (which it does, now that `process_frame` is present). |
| J6 | `asyncio.sleep(0.3)` "give ALSA threads time to die" | v3–v7 | Cargo-culted delay. Not needed when shutdown is clean. |
| J7 | `try/except CancelledError` in every `process_frame` | v3–v7 | Defensive noise. Pipecat's task manager handles cancellation. |
| J8 | `finally: await self.push_frame(frame, direction)` — double push | v3–v7 | Tried to ensure frame propagation even on cancellation. Actually pushed every frame twice on the happy path. |
| J9 | TRIPWIRE debug prints | v5–v7 | Diagnostic noise. |
| J10 | `asyncio.create_task(runner.run(...))` + `shutdown_event.wait()` | v7 | Tried to replicate diagnostic v4's pattern inside Pipecat. Unnecessary — `await runner.run(task)` is the correct pattern. |

### What v8 got right vs. wrong

v8 correctly applied W1–W5, W7–W9 and correctly discarded J1–J10. Its **only error** was discarding `process_frame` from `DeepgramSTTProcessor` (violating W6). The method looked like a no-op pass-through, so it was removed during cleanup — but it was load-bearing because Pipecat's base class doesn't push frames.

---

## 2. Known Bug: Buffer Overflow in Diagnostic Baseline

Diagnostic v4's `audio_callback` uses `np.append(utterance_buffer, audio_chunk)` to accumulate captured audio. `np.append` is O(n) — it allocates a new array and copies the entire buffer on every chunk (~every 80ms at 1280 samples / 16kHz). This runs **inside the PyAudio callback thread**, which must return before the next chunk arrives.

**Short utterances (~1s):** Buffer stays small (~32KB). Copy completes well within the 80ms deadline. Works fine.

**Long utterances (~8s+):** Buffer grows to ~256KB. Copy time approaches and exceeds the 80ms chunk interval. PyAudio's internal ring buffer overflows. On ALSA-only Pi 4 with USB audio (ReSpeaker), this can:
1. Produce **static/noise** from corrupted or dropped samples
2. Cascade ALSA `snd_pcm_readi` underruns into the USB audio driver
3. Hang the USB audio device, requiring a **reboot**

This bug is orthogonal to the shutdown problem — it does not affect whether Ctrl+C produces a clean exit. It affects whether the audio subsystem survives a long capture. The fix is straightforward (switch to `list.append` + single `np.concatenate` at transcription time) but is **deferred** to avoid confounding the shutdown investigation.

**Mitigation in practice:** To minimize reboot risk, the tester says a single short word ("hello") immediately after wake detection and presses Ctrl+C with minimal delay. This keeps the buffer small and reduces exposure to the overflow.

**Impact on this iteration sequence:** The checkpoint protocol uses short test sentences (~1–2s). The overflow will not trigger during shutdown testing. Each increment carries this bug forward from the baseline; it gets fixed once the shutdown issue is resolved, or earlier if a test step requires longer utterances. If static or a reboot occurs during testing, suspect this bug rather than the increment under test.

**Observed:** v1 testing (2026-03-31) triggered a buffer overflow reboot on the first execution attempt. Second attempt succeeded. This confirms the bug is live and can strike even during short tests — likely when the user speaks longer than expected or the wake word detector takes time to trigger capture end.

---

## 2a. Session-State Deposition Protocol

**Problem:** Buffer overflow reboots (Section 2) and context-window overflows both destroy in-flight session state. If a reboot or context reset occurs mid-iteration, the recovering session has no way to know what was tested, what passed, or what the next step should be — unless that state is already written to disk.

**Strategy:** Before each test execution, deposit all relevant session state into this guide. The guide becomes the single source of truth that any future session — whether recovering from a reboot, a context overflow, or simply a new conversation — can pick up and continue from.

**What to deposit before each test:**

| Item | Where in this guide |
|---|---|
| Which increment is being tested next | Progression Tracker (Section 7) — mark as `TESTING` |
| The script filename and whether it exists on disk | Increment Sequence (Section 6) — confirm file written |
| Any decisions made that affect subsequent increments | Decision Points (Section 8) — record if triggered |
| Outcome of the previous increment | Progression Tracker (Section 7) — fill Pass/Fail row |
| Gate verdict for the previous increment | Increment Sequence (Section 6) — fill Actual outcome / Verdict |

**When to deposit:** After the previous increment is validated and before the user executes the next test. This is a **blocking step** — do not ask the user to run a test until the deposition is written and confirmed.

**Recovery procedure:** On session start after a crash or context reset, read this guide. The Progression Tracker shows where we are. Any row marked `TESTING` was in flight when the crash occurred — re-test it. Any row with a recorded outcome is settled.

---

## 3. Learnings from Prior Attempts (Shutdown)

Three structural differences exist between the working diagnostic v4 and the failing Pipecat v8. Ranked by suspicion:

### HIGH — RTVIProcessor is auto-injected
`PipelineTask.__init__()` defaults to `enable_rtvi=True` (task.py line 234). This silently adds an `RTVIProcessor` to the pipeline. Every `CancelFrame` must pass through this unrequested processor. If it doesn't propagate cancel correctly, the pipeline hangs. **v8 never set `enable_rtvi=False`.**

### HIGH — PyAudio callback thread race
`LocalAudioTransport` runs a PyAudio callback in a separate thread. The callback calls `run_coroutine_threadsafe()` to push frames into the asyncio pipeline. On Ctrl+C, the pipeline begins cancellation, but the PyAudio thread may still be pushing frames into a half-cancelled pipeline. In diagnostic v4, the signal handler explicitly stops the audio stream *before* async cleanup — Pipecat's `PipelineRunner` does not guarantee this ordering.

### MEDIUM — 20-second cancel timeout
Pipecat's `PipelineTask` has a hardcoded ~20-second timeout for `CancelFrame` propagation. If `CancelFrame` gets stuck at any processor, the pipeline hangs for the full timeout before forcibly terminating. This isn't the root cause but amplifies the impact of any frame-propagation bug.

---

## 4. Pass/Fail Criteria

A script **passes** if all of the following are true:

| Criterion | Requirement |
|---|---|
| Exit timing | Process terminates within **3 seconds** of Ctrl+C |
| Exit method | No `Terminated` message (i.e., not killed by SIGTERM/SIGKILL) |
| Exit code | `echo $?` returns **0** |
| Final transcription | If audio was captured before Ctrl+C, transcription prints before exit |
| No zombie | `ps aux | grep voice_pipeline` shows no remaining process |

A script **fails** if any criterion is not met. Record which criterion failed.

---

## 5. Checkpoint Protocol

Execute these steps identically for every increment.

**Pre-test:** Complete the session-state deposition (Section 2a) before proceeding. The Progression Tracker must show the current increment as `TESTING` and the previous increment's outcome must be recorded.

```
1.  ssh morpheus
2.  source ~/pipecat-agent/venv/bin/activate
3.  cd ~/pre-design-demos/e2e-pipeline-integration
4.  python <script_filename>.py
5.  Wait for "Listening..." message
6.  Say "hey Jarvis" clearly at ~1m distance
7.  After wake detection prints, speak a short test sentence (e.g., "What time is it?")
8.  Wait 2 seconds after finishing speaking
9.  Press Ctrl+C once
10. Observe:
    - Does the process print shutdown messages?
    - Does the final transcription run?
    - Does the shell prompt return within 3 seconds?
    - What is `echo $?`?
11. Run: ps aux | grep voice_pipeline
12. Record result in the Progression Tracker (Section 7)
```

Run each increment **twice** to confirm consistency. If results differ between runs, run a third time and note the inconsistency.

---

## 6. Increment Sequence

> **Gate rule:** Each increment's script is only created after the previous increment has been tested and its outcome recorded. Do not write ahead — the result of each step determines what the next script should contain (e.g., RTVI setting, which processors to include, whether to proceed at all).

### v1 — Baseline confirmation

**File:** `voice_pipeline_step6_incremental_v1.py`
**Adds:** Diagnostic v4 verbatim (copy-paste, rename only)
**Hypothesis:** Baseline still exits cleanly after reboot / venv changes.
**Expected outcome:** PASS
**Actual outcome:** PASS (2026-03-31). First run triggered buffer overflow reboot (Section 2). Second run exited cleanly — process terminated within 3s of Ctrl+C, exit code 0, no zombie.
**Verdict:** PASS — baseline confirmed. Buffer overflow reboot on first attempt is orthogonal (Section 2), not a shutdown failure.

> **Gate:** ✅ v1 PASSED. Proceed to v2.

---

### v2 — Minimal Pipecat framework wrapping

**File:** `voice_pipeline_step6_incremental_v2.py`
**Adds:** Wrap the PyAudio input in `LocalAudioTransport` + `Pipeline` + `PipelineTask` + `PipelineRunner`. Pipeline contains only `transport.input()`. No custom processors. **`enable_rtvi=False`** explicitly.
**Hypothesis:** The minimal Pipecat framework (runner, task, transport) can exit cleanly when RTVI is disabled.
**Expected outcome:** PASS if Pipecat's core framework handles CancelFrame correctly; FAIL if the PyAudio thread race is the root cause.
**Actual outcome:** PASS (2026-03-31). CancelFrame reached end of pipeline in ~1ms. Process exited cleanly with "finished cleanly" message. ALSA/JACK/PulseAudio warnings are noise (no PulseAudio/JACK on this Pi — harmless).
**Verdict:** PASS — minimal Pipecat framework (PipelineRunner + PipelineTask + LocalAudioTransport, RTVI OFF) exits cleanly on Ctrl+C.

> **Gate:** ✅ v2 PASSED. Proceed to v2a (RTVI test).

---

### v2a — RTVIProcessor test (conditional — only if v2 passes)

**File:** `voice_pipeline_step6_incremental_v2a.py`
**Adds:** Same as v2, but with `enable_rtvi=True` (Pipecat default).
**Hypothesis:** RTVIProcessor blocks CancelFrame propagation.
**Expected outcome:** FAIL (RTVIProcessor is the prime suspect)
**Actual outcome:** PASS (2026-03-31). RTVIProcessor#0 was auto-injected (visible in linking logs: Source -> RTVIProcessor#0 -> Pipeline#0). CancelFrame propagated through RTVIProcessor in ~2ms. Clean exit.
**Verdict:** PASS — RTVIProcessor does NOT block CancelFrame. It is not the shutdown culprit.

> **Gate:** ✅ v2a PASSED. RTVIProcessor cleared. Subsequent increments use `enable_rtvi=True` (default). Proceed to v3.

---

### v3 — Add OpenWakeWordProcessor

**File:** `voice_pipeline_step6_incremental_v3.py`
**Adds:** `OpenWakeWordProcessor` in the pipeline after `transport.input()`. Uses the same implementation as v8.
**Hypothesis:** Wake word processing does not block CancelFrame.
**Expected outcome:** PASS
**Actual outcome:** PASS (2026-03-31). First run triggered buffer overflow reboot (Section 2 — `Connection reset` at end of SSH session). Second run exited cleanly — CancelFrame propagated through OpenWakeWordProcessor in ~3ms, "finished cleanly" message printed, shell prompt returned immediately.
**Verdict:** PASS — OpenWakeWordProcessor does NOT block CancelFrame. Buffer overflow reboot on first attempt is orthogonal (Section 2).

> **Gate:** ✅ v3 PASSED. Proceed to v4.

---

### v4 — Add UtteranceCapturer

**File:** `voice_pipeline_step6_incremental_v4.py`
**Adds:** `UtteranceCapturer` after `OpenWakeWordProcessor`.
**Hypothesis:** Capture buffering does not block CancelFrame.
**Expected outcome:** PASS
**Actual outcome:** PASS (2026-03-31). First run triggered buffer overflow reboot (Section 2 — `Connection reset` at end of SSH session). Second run exited cleanly — CancelFrame propagated through UtteranceCapturer in ~2ms, "finished cleanly" message printed, captured 22720 samples, shell prompt returned immediately.
**Verdict:** PASS — UtteranceCapturer does NOT block CancelFrame. Buffer overflow reboot on first attempt is orthogonal (Section 2).

> **Gate:** ✅ v4 PASSED. Proceed to v5.

---

### v5 — Add DeepgramSTTProcessor + post-pipeline transcription

**File:** `voice_pipeline_step6_incremental_v5.py`
**Adds:** `DeepgramSTTProcessor` after `UtteranceCapturer`, plus post-`runner.run()` transcription call. This matches v8's full feature set.
**Hypothesis:** Full feature set exits cleanly now that the framework-level blocker (if any) is resolved.
**Expected outcome:** PASS
**Actual outcome:** PASS (2026-03-31). First run triggered buffer overflow reboot (Section 2 — `Connection reset` at end of SSH session). Second run exited cleanly — CancelFrame propagated through full pipeline (including DeepgramSTTProcessor) in ~4ms. Post-pipeline transcription ran successfully (transcript: "Hello?"). "finished cleanly" message printed, shell prompt returned immediately.
**Verdict:** PASS — Full pipeline (LocalAudioTransport + OpenWakeWordProcessor + UtteranceCapturer + DeepgramSTTProcessor, RTVI ON) exits cleanly on Ctrl+C. Post-pipeline transcription completes before exit.

> **Gate:** ✅ v5 PASSED. All increments pass. Jump to **Decision Point D** — v5 is the new production baseline. Diff v5 against v8 to identify root cause of v8's hang.

---

## 7. Progression Tracker

| Inc | File | RTVI | Pass/Fail | Exit Time | Exit Code | Final Transcription | Notes |
|-----|------|------|-----------|-----------|-----------|---------------------|-------|
| v1 | _incremental_v1.py | N/A | PASS | <3s | 0 | N/A (baseline) | 1st run: buffer overflow reboot. 2nd run: clean exit. |
| v2 | _incremental_v2.py | OFF | PASS | <1s | 0 | N/A | CancelFrame propagated in ~1ms. Clean exit. |
| v2a | _incremental_v2a.py | ON | PASS | <1s | 0 | N/A | RTVIProcessor cleared. CancelFrame propagated ~2ms. |
| v3 | _incremental_v3.py | ON | PASS | <1s | 0 | N/A | 1st run: buffer overflow reboot. 2nd run: clean exit. CancelFrame ~3ms. |
| v4 | _incremental_v4.py | ON | PASS | <1s | 0 | N/A | 1st run: buffer overflow reboot. 2nd run: clean exit. CancelFrame ~2ms. 22720 samples captured. |
| v5 | _incremental_v5.py | ON | PASS | <1s | 0 | "Hello?" | 1st run: buffer overflow reboot. 2nd run: clean exit. CancelFrame ~4ms. Transcription ran post-pipeline. |
| | | | | | | | **— Phase 2: v5→v8 alignment —** |
| v6 | _incremental_v6.py | ON | PASS | <1s | 0 | "Hello." | Import timing alignment (A+B+C). CancelFrame ~4ms. Clean exit. |
| v7 | _incremental_v7.py | ON | PASS | <1s | 0 | "Hello." | Class definition order (D). CancelFrame ~3ms. Clean exit. |
| v8a | _incremental_v8a.py | ON | FAIL | N/A | SIGTERM | None | StartFrame stuck. process_frame removal + rename + comment. Splitting. |
| v8b | _incremental_v8b.py | ON | FAIL | N/A | SIGTERM | None | process_frame removal alone causes hang. ROOT CAUSE ISOLATED. |
| v9 | _incremental_v9.py | ON | PASS | <1s | 0 | (yes) | Clean production script. 1st run: buffer overflow reboot. 2nd run: clean exit. |
| v10 | _incremental_v10.py | ON | PASS | <1s | 0 | "12345678910111213141516." | Buffer overflow fix. ~24s capture, no reboot, clean exit. |

\* RTVI setting for v3–v5 depends on v2a outcome. OFF if v2a fails, ON (default) if v2a passes.

---

## 8. Decision Points

### Decision Point A — v2 fails (minimal Pipecat can't exit cleanly)

**Meaning:** `PipelineRunner` + `LocalAudioTransport` cannot produce a clean Ctrl+C exit regardless of pipeline contents. The problem is in Pipecat's core framework or its interaction with PyAudio's callback thread.

**Action:**
1. Abandon the Pipecat `PipelineRunner` for shutdown control.
2. Use diagnostic v4's direct-PyAudio pattern as the production base.
3. Add wake word and STT as **plain async functions** called from the audio callback or main loop — not as `FrameProcessor` subclasses.
4. Pipecat remains available for non-shutdown concerns (frame types, processor base class) but `PipelineRunner.run()` is not used as the top-level event loop.
5. Document this as a Pipecat caveat in the final verdict.

### Decision Point B — v2 passes, v2a fails (RTVIProcessor is the blocker)

**Meaning:** Pipecat's core is fine, but the auto-injected RTVIProcessor breaks CancelFrame propagation.

**Action:**
1. **Fix:** Always pass `enable_rtvi=False` to `PipelineTask`.
2. All subsequent increments and the production script use `enable_rtvi=False`.
3. If RTVI functionality is ever needed, investigate the processor's `process_frame` for cancel handling. For now, it's not needed.
4. Continue the increment sequence with this fix applied.

### Decision Point C — v2 and v2a both pass, breakage at v3/v4/v5

**Meaning:** A specific custom processor blocks CancelFrame.

**Action:**
1. Inspect the failing processor's `process_frame()` method.
2. Ensure it calls `await self.push_frame(frame, direction)` for **all** frame types, including `CancelFrame`.
3. Add explicit cancel handling if needed:
   ```python
   from pipecat.frames.frames import CancelFrame
   if isinstance(frame, CancelFrame):
       # cleanup, then push
       await self.push_frame(frame, direction)
       return
   ```
4. Re-test. If it passes, continue the sequence.

### Decision Point D — All increments pass

**Meaning:** The incremental build exits cleanly but v8 (which was built monolithically) did not. The difference is likely a subtle code issue in v8 that was avoided by careful incremental construction.

**Action (revised 2026-03-31):**
v5 and v8 are functionally equivalent but v8 still hangs consistently (confirmed with 3 additional runs: ~50% CPU, requires SIGTERM). The diff reveals non-cosmetic differences in import ordering and initialization timing. Rather than declaring victory on v5, continue the incremental alignment: mutate v5 toward v8 one cluster at a time until behavior diverges. The first cluster that breaks shutdown isolates the root cause.

---

## 10. Phase 2 — Alignment Sequence (v5 → v8 convergence)

> **Purpose:** v5 exits cleanly, v8 hangs. Code is functionally identical. The diff contains import-time, ordering, and cosmetic differences. This phase mutates v5 toward v8 one cluster at a time. The first cluster that introduces the hang isolates the cause.
>
> **Gate rule:** Same as Phase 1 — each increment's script is only created after the previous one is tested and recorded.
>
> **Diff inventory** (substantive differences between v5 and v8):
>
> | ID | Category | v5 (clean exit) | v8 (hangs) |
> |----|----------|-----------------|------------|
> | A | openwakeword import | Lazy — inside `__init__` | Top-level — at module load |
> | B | DeepgramClient import | After `ORT_LOG_LEVEL` set | Top-level — before `ORT_LOG_LEVEL` |
> | C | `ORT_LOG_LEVEL` placement | Before pipecat/deepgram imports | After all imports |
> | D | Class definition order | UtteranceCapturer → OWW → DeepgramSTT | OWW → UtteranceCapturer → DeepgramSTT |
> | E | Method name | `transcribe_sync` | `_transcribe_sync` |
> | **E2** | **`process_frame` on DeepgramSTT** | **Explicit override (super + push)** | **Missing — relies on base class default** |
> | F | Cosmetics | Plain text, docstrings | Emojis, no docstrings, inline comments |
>
> **Ordering rationale:** A/B/C are tested first because they change what runs at import time (before `asyncio.run`). D/E/F are unlikely to affect behavior but must be eliminated to reach empty diff.
>
> **ROOT CAUSE (isolated 2026-03-31):** Difference **E2** — the missing `process_frame` override on `DeepgramSTTProcessor`. See Section 1a for full analysis. This was not in the original diff inventory because it appeared cosmetic (the override only called super + push). In fact, `FrameProcessor.process_frame` does not push frames — every subclass must push explicitly.

---

### v6 — Import timing alignment (clusters A + B + C)

**File:** `voice_pipeline_step6_incremental_v6.py`
**Changes from v5:**
1. Move `from openwakeword.model import Model` to top-level (match v8). Remove lazy import from `__init__`.
2. Move `from deepgram import DeepgramClient` to top-level, before `ORT_LOG_LEVEL` (match v8).
3. Move `os.environ["ORT_LOG_LEVEL"] = "ERROR"` to after all imports (match v8 line 25).
**Hypothesis:** Top-level import of openwakeword or deepgram (which initialize onnxruntime threads or signal handlers before `asyncio.run`) is the root cause of v8's hang.
**Expected outcome:** FAIL — this is the highest-suspicion cluster.
**Actual outcome:** PASS (2026-03-31). CancelFrame propagated through full pipeline in ~4ms. Post-pipeline transcription ran successfully (transcript: "Hello."). "finished cleanly" message printed, shell prompt returned immediately.
**Verdict:** PASS — Import timing (top-level openwakeword/deepgram imports, ORT_LOG_LEVEL after imports) does NOT cause the hang. Not the root cause.

> **Gate:** ✅ v6 PASSED. Import timing cleared. Proceed to v7.

---

### v7 — Class definition order (cluster D)

**File:** `voice_pipeline_step6_incremental_v7.py`
**Changes from v6:** Reorder class definitions to match v8: OpenWakeWordProcessor → UtteranceCapturer → DeepgramSTTProcessor.
**Hypothesis:** Class definition order does not affect runtime behavior.
**Expected outcome:** PASS
**Actual outcome:** PASS (2026-03-31). CancelFrame propagated through full pipeline in ~3ms. Post-pipeline transcription ran successfully (transcript: "Hello."). "finished cleanly" message printed, shell prompt returned immediately.
**Verdict:** PASS — Class definition order does NOT affect shutdown. Not the root cause.

> **Gate:** ✅ v7 PASSED. Proceed to v8a.

---

### v8a — Method naming + code comments (cluster E)

**File:** `voice_pipeline_step6_incremental_v8a.py`
**Changes from v7:** Rename `transcribe_sync` → `_transcribe_sync`. Add v8's inline comment above `runner = PipelineRunner()`.
**Hypothesis:** Method naming does not affect runtime behavior.
**Expected outcome:** PASS
**Actual outcome:** FAIL (2026-03-31). StartFrame never reached end of pipeline ("pipeline is now ready" log absent). CancelFrame also stuck. Process required SIGTERM (`Terminated`). Three changes were bundled: method rename, comment addition, and removal of explicit `process_frame` from DeepgramSTTProcessor.
**Verdict:** FAIL — one of the three changes breaks shutdown. Splitting into v8b (process_frame removal only) to isolate.

> **Gate:** ❌ v8a FAILED. Splitting changes to isolate. v8b tests process_frame removal alone.

---

### v9 — Clean production script (first attempt + wanted changes only)

**File:** `voice_pipeline_step6_incremental_v9.py`
**Base:** `voice_pipeline_step6_first_attempt.py`, applying only the wanted changes (W1–W9 from Section 1b). No troubleshooting jitter (J1–J10). Specifically:
- W1: `dotenv` + API key check
- W2: `time.time()` for debounce
- W3: `start_capture()` resets buffer
- W4: Post-pipeline `_transcribe_sync` (remove inline transcription from `process_frame`)
- W5: Remove bare `import openwakeword`
- W6: `process_frame` on all three processors (kept from first attempt)
- W7: `PipelineRunner` handles SIGINT (default)
- W8: Plain `await runner.run(task)` — no try/except wrapper
- W9: No manual `transport.cleanup()`
**Hypothesis:** First attempt + wanted changes exits cleanly.
**Expected outcome:** PASS
**Actual outcome:** PASS (2026-03-31). 1st run: buffer overflow reboot (Section 2). 2nd run: clean exit.
**Verdict:** PASS — clean production script confirmed. Proceed to v10.

> **Gate:** ✅ v9 PASSED. Proceed to v10 (buffer overflow fix).

---

### v10 — Write out buffer overflow

**File:** `voice_pipeline_step6_incremental_v10.py`
**Changes from v9 (or from whichever increment is the final aligned baseline):**
1. `UtteranceCapturer`: Replace `np.append(self.utterance_buffer, audio_chunk)` with `self._chunks.append(audio_chunk)` (list append). Add `self._chunks = []` to `__init__` and `start_capture`. In a new property or method, concatenate with `np.concatenate(self._chunks)` only when transcription is requested.
2. `OpenWakeWordProcessor`: Replace `np.append(self.buffer, audio_chunk)` with the same list-append pattern. Concatenate only when feeding to `model.predict`.
**Hypothesis:** Buffer overflow is eliminated. Long utterances (~8s+) no longer cause static/reboot.
**Expected outcome:** PASS on shutdown (clean exit). PASS on long utterance (no static, no reboot).
**Test protocol:** Run twice with short utterance (shutdown test per Section 5). Then run once with ~8 second utterance to confirm no static/reboot.
**Actual outcome:** PASS (2026-03-31). ~24 seconds of continuous audio capture (counting 1–16). No static, no reboot, no buffer overflow. Clean exit — CancelFrame propagated in ~4ms. Transcript: "12345678910111213141516." (conjoined numerals are Deepgram smart_format rendering, not a pipeline issue).
**Verdict:** PASS — Buffer overflow fix confirmed. This is the production baseline.

> **Gate:** ✅ v10 PASSED. Production baseline established. Step 6 complete.

---

## 11. Decision Points — Phase 2

### Decision Point E — v6 fails (import timing is the root cause)

**Meaning:** Moving openwakeword/deepgram imports to top-level (before `asyncio.run`) breaks clean exit. One of these libraries installs a signal handler or starts a thread that interferes with Pipecat's SIGINT handling.

**Action:**
1. Create v6a (only openwakeword top-level), v6b (only deepgram top-level), v6c (only ORT_LOG_LEVEL move) to isolate which import.
2. Once isolated, document: "Library X must be imported inside `asyncio.run()`, not at module level, to avoid interfering with Pipecat's signal handling."
3. Apply the fix to the production script.

### Decision Point F — All alignment increments pass, v9 matches v8, but v8 itself still hangs

**Meaning:** The hang is environmental or path-dependent (e.g., Python caches, .pyc files, filename-based behavior). v9 is byte-identical code but runs from a different filename.

**Action:**
1. Copy v9 to a file named exactly `voice_pipeline_step6_pipecat_shutdown_v8.py` (overwrite v8).
2. Test the overwritten v8. If it passes, the original v8 had a stale .pyc or the filename was irrelevant.
3. If it still fails, investigate `__pycache__` and `.pyc` corruption.

### Decision Point G — v9 matches v8 and v8 now passes on re-test

**Meaning:** The original v8 hang was transient — caused by system state at the time of testing (e.g., onnxruntime worker threads left from a prior crash, USB audio driver in a degraded state post-reboot).

**Action:**
1. Accept v10 (with buffer overflow fix) as the production baseline.
2. Document: "v8's hang was environmental, not code-caused. The buffer overflow (Section 2) was likely leaving the system in a degraded state that made subsequent runs hang."

---

## 12. File Naming Convention (updated)

All increment scripts live in the same directory as this guide:

```
~/pre-design-demos/e2e-pipeline-integration/
    # Phase 1 — Component isolation
    voice_pipeline_step6_incremental_v1.py   # Baseline (diagnostic v4 copy)
    voice_pipeline_step6_incremental_v2.py   # Minimal Pipecat, RTVI off
    voice_pipeline_step6_incremental_v2a.py  # Minimal Pipecat, RTVI on
    voice_pipeline_step6_incremental_v3.py   # + OpenWakeWordProcessor
    voice_pipeline_step6_incremental_v4.py   # + UtteranceCapturer
    voice_pipeline_step6_incremental_v5.py   # + DeepgramSTTProcessor (full feature set)
    # Phase 2 — v5→v8 alignment
    voice_pipeline_step6_incremental_v6.py   # Import timing alignment (A+B+C)
    voice_pipeline_step6_incremental_v7.py   # Class definition order (D)
    voice_pipeline_step6_incremental_v8a.py  # Method naming + comments (E)
    voice_pipeline_step6_incremental_v9.py   # Cosmetic alignment (F) — target: empty diff vs v8
    voice_pipeline_step6_incremental_v10.py  # Buffer overflow fix
```

Each script is **self-contained** — no imports between increments. Working code is copy-pasted forward. This avoids debugging import chains on top of shutdown issues.
