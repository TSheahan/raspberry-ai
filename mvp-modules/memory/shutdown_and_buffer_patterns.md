# Shutdown and Buffer Patterns

Proven root causes, fixes, and anti-patterns for audio pipeline stability on Raspberry Pi 4. Extracted from step 6 (buffer overflow) and step 7 (shutdown crash) investigations.

## The 20ms Frame Clock

PyAudio's callback thread produces one `InputAudioRawFrame` every **20ms**, driven by the ALSA/I2S audio subsystem. This is hardware-paced and not governed by the asyncio event loop. Frames are pushed via `asyncio.run_coroutine_threadsafe()`.

Any per-frame work exceeding 20ms causes queue growth. Pipecat uses unbounded `asyncio.Queue` at every pipeline level — no backpressure, no dropping, no bound. Growth is silent until it cascades.

## Root Cause 1: Buffer Overflow from O(n) Array Copy

`np.append(buffer, chunk)` allocates a new array and copies the entire buffer on every audio chunk (~every 80ms for OWW). As the buffer grows, copy time approaches and then exceeds the chunk interval. On Pi 4 with USB audio (ReSpeaker), this cascades into ALSA underruns that can hang the USB audio device, requiring a reboot.

**Fix:** `list.append(chunk)` for accumulation (O(1) per chunk). `np.concatenate(chunks)` only at consumption time — once per `model.predict()` call, once at transcription time. Never in the hot path.

**Applies to:** Any audio accumulation in `process_frame` or callback paths.

## Root Cause 2: CPU Starvation → I2S Audio Cascade → Reboot

When the Claude subprocess runs (7–12s), it consumes significant CPU. Any ONNX inference still running during that window competes for remaining cycles. Frames accumulate in unbounded queues. On Ctrl+C, Pipecat attempts to flush queues while PyAudio's callback thread keeps firing and the I2S/DMA transfer layer is starved of CPU attention. Hard reboot.

**Fix (single-process):** Phase-gate all ONNX workloads:
- **LISTENING:** OWW runs, Silero does not
- **CAPTURING:** Silero runs, OWW does not
- **PROCESSING (cognitive loop):** neither runs

**Fix (two-process, current architecture):** Recorder child on dedicated core 0 with SCHED_FIFO priority. Master on cores 1–3. No CPU competition. Ring buffer replaces unbounded queues. Idle phase gates off all ONNX inference during the cognitive loop (see Root Cause 2a below).

## Root Cause 2a: OWW Running During Cognitive Loop (Two-Process, Solved 2026-04-03)

In the two-process architecture, the master previously sent `SET_WAKE_LISTEN` before entering the cognitive loop so the recorder could listen while Claude processed. This caused OWW predict (22–32ms per call, 1 call per 80ms) to run at full rate during the entire ~10s Claude window — pure waste, as any wake detection during that window is discarded by the master (`processing=True` guard).

**Measured impact (2026-04-03):** duty cycle during cognitive loop: mean 3.1–7.1ms, max **49.6ms** (2.5× the 20ms frame budget). With the fix: max **3.0ms** (16× reduction).

**Fix: idle phase.** New RecorderState phase `"idle"` sits between `capture` and `wake_listen`. Stream stays active, audio continues writing to the ring buffer, but both OWW (`wake_listen` gate) and Silero (`capture` gate) are fully inactive. Protocol:

```
VAD_STOPPED → master sends SET_IDLE → cognitive loop (STT + Claude) → master sends SET_WAKE_LISTEN
```

The `SET_WAKE_LISTEN` is sent in a `finally` block to ensure it fires even on cognitive loop exception.

**Confirmed (2026-04-03, run with SCHED_FIFO):** idle phase duty cycle windows: mean 1.2–1.7ms, max 3.0ms, util 6–9%. q_max=0 throughout.

## Root Cause 3: Shutdown Race (Single-Process, Unresolvable)

After a cognitive loop has run, Ctrl+C triggers a race between PyAudio's callback thread and asyncio's cancellation path. The crash occurs in PortAudio's C layer during teardown — beyond Python-level control.

**Symptom:** Ctrl+C before any cognitive loop always exits cleanly. After a cognitive loop, Ctrl+C causes `client_loop: send disconnect: Connection reset` (Pi reboots).

**Mitigation (single-process):** Pause PyAudio stream during cognitive loop. Stop stream on `CancelFrame` before normal cancel path runs. These reduce the window but do not eliminate the race.

**Partial resolution:** Two-process architecture. Recorder child tears down independently; a killed child doesn't reboot the Pi. But the race can still occur *within* the child if SIGINT reaches it directly and triggers `task.cancel()` without first stopping the stream. See Root Cause 4 below.

## Root Cause 4: SIGINT in Child Bypasses Stream-Stop Ordering (Two-Process)

In the two-process architecture, `^C` delivers SIGINT to the **entire process group** — both master and child simultaneously. If the child's SIGINT handler calls `task.cancel()` directly, the `CancelFrame` path may call `stop_stream()` while the PortAudio callback thread is still active. This is the same USB race as Root Cause 3, now occurring within the child.

**Symptom (2026-04-03, run 2):** Pi rebooted on Ctrl+C after one successful voice turn. Child's `finally` block never ran — no QDEPTH summary, no `[child] exiting`. `client_loop: send disconnect: Connection reset`.

**Critical ordering invariant:** `stop_stream()` must complete (with a settle period) **before** `task.cancel()` is called. The ALSA/PortAudio callback thread must be quiesced before CancelFrame propagates through the pipeline.

**Fix — two-phase shutdown protocol (proven 2026-04-03):** A single `_initiate_shutdown()` coroutine with a once-only guard handles SIGINT, SIGTERM, and the SHUTDOWN pipe command:

```
1. Send SHUTDOWN_COMMENCED to master (over pipe)
2. state.set_phase("dormant") — stops PyAudio stream, 100ms settle
3. task.cancel() — CancelFrame propagates against a stopped audio source
4. Print diagnostics (QDEPTH, duty cycle)
5. Send SHUTDOWN_FINISHED to master
6. Close SharedMemory, exit process
```

The master waits for SHUTDOWN_FINISHED on all exit paths (KeyboardInterrupt, normal return, EOFError) before cleaning up SharedMemory. SIGINT is deferred at process entry (`SIG_IGN`) to close the narrow window before the event loop installs its handler.

**Confirmed clean shutdown (2026-04-03, run 3):** QDEPTH summary printed, full duty cycle summary printed, SHUTDOWN_FINISHED received by master, clean shell prompt, no reboot.

## Root Cause 5: Shutdown Crash — Mechanism Misidentified, Then Corrected (2026-04-03)

**⚠ This root cause was originally documented with a wrong causal mechanism. The symptoms and observations are accurate; the explanation was wrong. See correction below.**

### Original (incorrect) explanation

Root Causes 3–5 were attributed to `Pa_StopStream()` / `Pa_CloseStream()` / `Pa_Terminate()` interacting with the **USB** subsystem on Pi 4, causing xHCI driver faults, USB host controller resets, and system reboots.

### Correction (2026-04-03, session 6+)

The ReSpeaker 4-Mic Array HAT uses **I2S** (GPIO header), not USB. The audio path through the kernel is:

```
PortAudio → ALSA libasound → snd_pcm kernel core → ASoC framework
  → seeed-voicecard.c (machine driver)
  → ac108.c (AC108 codec driver, I2C register control)
  → BCM2835 I2S + DMA engine → GPIO pins
```

**There is no USB, no xHCI, and no URB in this path.** Every reference to "USB fault", "xHCI driver", "USB isochronous transfers", and "USB host controller reset" in the prior RC5 analysis was wrong transport attribution.

**Why the crash persists despite os._exit(0):** `os._exit(0)` bypasses Python destructors but the kernel still closes all open file descriptors, including the ALSA PCM device FD. This triggers `snd_pcm_release()` → seeed-voicecard and ac108 driver teardown. The crash is in that kernel driver path. No Python-level mitigation can avoid it.

### What was actually observed and what it means

The symptom sequence across runs 4–6 (paComplete, skip close, os._exit) produced an apparent progression of the crash moving to later and later log lines — and `[state] stream stopped via paComplete` was genuinely observed. These observations are accurate. What they tell us:

- The paComplete pattern does successfully stop PortAudio from calling `Pa_StopStream()` from outside the callback. This is probably still beneficial — it avoids the PortAudio internal TRIGGER_STOP path that may interact poorly with the I2S/AC108 driver state machine.
- The crash that persists is in the driver's `shutdown`/`close` path, not in `Pa_StopStream`.
- `os._exit(0)` may avoid one Python-triggered close sequence, but the kernel still executes FD cleanup.

### Python mitigations currently in place (commit 140c9bd)

These remain in the codebase and should not be removed until driver-level analysis confirms what specifically causes the crash:

```
1. paComplete callback flag (_stop_producing) — avoids Pa_StopStream from event loop thread
2. cancel monkey-patch — idempotent flag check, no Pa_StopStream
3. cleanup monkey-patch — _in_stream = None only, no Pa_CloseStream
4. os._exit(0) in recorder_child_entry
```

### Next investigation direction

The crash must be fixed at the driver level. Key suspects in `seeed-voicecard` and `ac108` driver source:
- I2C operations inside `spin_lock_irqsave` in `ac108_trigger` (sleeping in atomic context → kernel BUG)
- Workqueue race between deferred clock stop and `ac108_aif_shutdown`
- No error handling on I2C writes in `ac108_aif_shutdown`

See handoff plan for full analysis scope.

**Expected clean shutdown log sequence (target, not yet achieved):**
```
[state] signaling callback paComplete...
[state] stream stopped via paComplete
[child] callback already signaled before cancel
[child] cleanup: skipped stream close (kernel will release)
[QDEPTH] max_depth_seen=...  total_alarms=...
(DUTY CYCLE SUMMARY if LOG_LEVEL=PERF)
[child] SHUTDOWN_FINISHED sent
[child] exiting
[master] done
$
```

## Monkey-Patch Pattern for Pipecat Transport

When Pipecat's transport behaviour must be changed without forking the package, monkey-patch at the instance level inside `recorder_child_main()`, before the pipeline is built. This scopes changes to one process run and leaves the installed package untouched.

```python
# Pattern: save original, wrap, reassign on instance
original = obj.method
def replacement(args):
    # different behaviour
    return original(args)   # optionally call through
obj.method = replacement
```

**Timing invariant:** patches must be applied after `transport.input()` returns the `LocalAudioInputTransport` instance, but before `Pipeline(processors)` and `runner.run(task)` — the latter triggers `StartFrame` which opens the PyAudio stream and registers the C-level callback pointer. The callback patch must be in place before the stream opens.

**Instance vs class scope:** `obj.method = f` only affects that object. `ClassName.method = f` would affect all instances in the process. Always patch the instance.

## OWW State Reset Protocol

OpenWakeWord's preprocessor accumulates state in five internal buffers: `prediction_buffer`, `raw_data_buffer`, `melspectrogram_buffer`, `feature_buffer`, `accumulated_samples`. All must be reset on every ungating transition (e.g., CAPTURE→WAKE_LISTEN).

Without reset, stale features combine with fresh audio to produce false-positive wake detections. Observed false positive score: 0.865 (threshold: 0.5).

## Silero LSTM Reset Ordering

Silero VAD maintains LSTM hidden states across frames. When resuming audio processing after a pause (e.g., after cognitive loop completes), reset hidden states **before** the first new frame arrives. If the stream resumes before the reset, stale hidden states contaminate the initial speech detection.

Implementation: `asyncio.create_task()` the reset-then-resume sequence, never synchronous from a Pipecat callback.

## Stream Lifecycle Anti-Patterns

| Anti-pattern | Consequence |
|---|---|
| Calling `stop_stream()`/`start_stream()` synchronously from `process_frame` | PortAudio deadlock or I2S/driver fault |
| Manual `transport.cleanup()` in `finally` blocks | Double-cleanup races with Pipecat teardown |
| SIGINT handler calling `task.cancel()` directly | Skips stream-stop ordering → same race as Root Cause 3/4 |
| Calling `Pa_StopStream()`/`stop_stream()` from non-callback thread | May trigger bad interaction with I2S/AC108 driver state machine (Root Cause 5) |
| Calling `Pa_CloseStream()`/`stream.close()` after stream stopped | Triggers driver teardown path — crash mechanism under investigation |
| Relying on Python GC / `PyAudio.__del__` for stream teardown | `Pa_Terminate()` triggers driver teardown even after os._exit is expected to help |
| `np.append` in any frame-processing hot path | O(n) copy → ALSA underrun → I2S hang → reboot |
| Unbounded queue without backpressure | Silent frame accumulation until cascade |
| Running ONNX inference (OWW/Silero) during cognitive loop | Unnecessary CPU load during Claude subprocess; duty cycle spikes to 49ms max |

## SCHED_FIFO for Recorder Child (2026-04-03)

The recorder child should run at real-time scheduling priority to prevent preemption by normal-priority processes during ONNX inference and I2S DMA servicing.

**Implementation in `recorder_child_entry`:**
```python
try:
    os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
except PermissionError:
    try:
        os.nice(-10)
    except PermissionError:
        pass  # log warning
```

**Prerequisite:** Add to `/etc/security/limits.d/99-realtime.conf` on Pi:
```
@audio   -  rtprio  99
user     -  rtprio  99
```
(Replace `user` with the actual Pi username.)

**Confirmed active (2026-04-03):** Log line `[child] SCHED_FIFO priority 50` on child startup.

**Effect:** Duty cycle variance under cognitive loop dropped from max 49.6ms to max 3.0ms in the same run that confirmed the idle phase fix. Both changes were applied together so individual attribution is not isolated.
