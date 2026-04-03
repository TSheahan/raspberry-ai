# Shutdown and Buffer Patterns

Proven root causes, fixes, and anti-patterns for audio pipeline stability on Raspberry Pi 4. Extracted from step 6 (buffer overflow) and step 7 (shutdown crash) investigations.

## The 20ms Frame Clock

PyAudio's callback thread produces one `InputAudioRawFrame` every **20ms**, driven by the ALSA/USB audio subsystem. This is hardware-paced and not governed by the asyncio event loop. Frames are pushed via `asyncio.run_coroutine_threadsafe()`.

Any per-frame work exceeding 20ms causes queue growth. Pipecat uses unbounded `asyncio.Queue` at every pipeline level — no backpressure, no dropping, no bound. Growth is silent until it cascades.

## Root Cause 1: Buffer Overflow from O(n) Array Copy

`np.append(buffer, chunk)` allocates a new array and copies the entire buffer on every audio chunk (~every 80ms for OWW). As the buffer grows, copy time approaches and then exceeds the chunk interval. On Pi 4 with USB audio (ReSpeaker), this cascades into ALSA underruns that can hang the USB audio device, requiring a reboot.

**Fix:** `list.append(chunk)` for accumulation (O(1) per chunk). `np.concatenate(chunks)` only at consumption time — once per `model.predict()` call, once at transcription time. Never in the hot path.

**Applies to:** Any audio accumulation in `process_frame` or callback paths.

## Root Cause 2: CPU Starvation → USB Audio Cascade → Reboot

When the Claude subprocess runs (7–12s), it consumes significant CPU. Any ONNX inference still running during that window competes for remaining cycles. Frames accumulate in unbounded queues. On Ctrl+C, Pipecat attempts to flush queues while PyAudio's callback thread keeps firing and the USB isochronous transfer layer is starved. The kernel panics or the USB host controller resets. Hard reboot.

**Fix (single-process):** Phase-gate all ONNX workloads:
- **LISTENING:** OWW runs, Silero does not
- **CAPTURING:** Silero runs, OWW does not
- **PROCESSING (cognitive loop):** neither runs

**Fix (two-process, current architecture):** Recorder child on dedicated core 0. Master on cores 1–3. No CPU competition. Ring buffer replaces unbounded queues.

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

## Root Cause 5: `Pa_StopStream()` Cross-Thread USB Fault (Two-Process)

Root Cause 4's fix ensures `stop_stream()` precedes `task.cancel()`. This is necessary but not sufficient. The `Pa_StopStream()` call itself — regardless of ordering — can trigger a USB subsystem fault on Pi 4 with ReSpeaker.

**Mechanism:** `stop_stream()` calls PortAudio's `Pa_StopStream()` from the asyncio event loop thread. The PortAudio callback thread is actively servicing USB isochronous transfers. `Pa_StopStream()` calls `snd_pcm_drop()`, which cancels pending USB URBs. If a URB completion interrupt is in flight during cancellation, the xHCI driver on Pi 4 faults. The kernel resets the USB host controller, which cascades to a system reboot.

**Symptom (2026-04-03, run 5):** Two-phase shutdown fires correctly — `_initiate_shutdown()` reaches `_stop_stream()` before `task.cancel()`. Log shows `[state] stream to stop..` but never `[state] stream stopped`. Pi reboots. Identical to Root Cause 4's symptom, but the ordering fix is in place.

**Why intermittent:** Timing-dependent race between `Pa_StopStream()` thread and USB transfer completion interrupts. Clean in run 4 (12:25), crashed in run 5 (14:17) with identical code.

**Additional hazard — triple `stop_stream()`:** Even if the first call survives, Pipecat's teardown calls `stop_stream()` up to three times: (1) `_stop_stream()` in `set_phase("dormant")`, (2) `cancel_with_stream_stop` during `task.cancel()`, (3) `LocalAudioInputTransport.cleanup()` after CancelFrame exits the pipeline. Each call independently risks the USB fault.

**Extended hazard — Pa_CloseStream and Pa_Terminate (2026-04-03, run 6):**
The paComplete fix confirmed: `[state] stream stopped via paComplete` appeared, `is_active()` returned False. But the crash then moved to `task.cancel()` → pipeline teardown → `LocalAudioInputTransport.cleanup()` → `stream.close()` → `Pa_CloseStream()`. Same xHCI race, different entry point. Additionally, Python's GC calls `PyAudio.__del__` → `pa_terminate()` → `Pa_Terminate()` which closes all open C-level stream handles on process exit — same fault vector even if Python `close()` is skipped.

**Complete fix — eliminate all three USB interaction points (2026-04-03):**

```
1. recorder_child_main: monkey-patch _audio_in_callback with guarded wrapper
   — _stop_producing = False initially; True signals the callback to return paComplete
2. _stop_stream: set _stop_producing = True, await 100ms, inspect is_active()
   — no Pa_StopStream call
3. cancel monkey-patch: idempotent flag check — no Pa_StopStream call
4. cleanup monkey-patch: _in_stream = None only — no Pa_CloseStream call
5. recorder_child_entry: os._exit(0) after asyncio.run
   — bypasses Python destructor cleanup, preventing Pa_Terminate from closing
     the C-level stream handle during GC
```

`os._exit(0)` fires after all Python-level shutdown work completes inside `asyncio.run` (QDEPTH summary, SHUTDOWN_FINISHED, `shm.close()`). The kernel releases the USB device during process teardown — the most stable path available.

**Confirmed diagnostic (run 6):** `[state] stream stopped via paComplete` — paComplete worked, is_active() False, 100ms was sufficient. Crash moved to stream.close(). Second commit eliminated close() and added os._exit(0).

**Expected clean shutdown log sequence:**
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
| Calling `stop_stream()`/`start_stream()` synchronously from `process_frame` | PortAudio deadlock or USB fault |
| Manual `transport.cleanup()` in `finally` blocks | Double-cleanup races with Pipecat teardown |
| SIGINT handler calling `task.cancel()` directly | Skips stream-stop ordering → same USB race as Root Cause 3/4 |
| Calling `Pa_StopStream()`/`stop_stream()` from non-callback thread | USB fault on Pi 4 xHCI → kernel reset → reboot (Root Cause 5) |
| Calling `Pa_CloseStream()`/`stream.close()` after stream stopped | Same xHCI race as stop_stream — crash moved here after paComplete fix |
| Relying on Python GC / `PyAudio.__del__` for stream teardown | `Pa_Terminate()` closes C-level handle → same USB fault on process exit |
| `np.append` in any frame-processing hot path | O(n) copy → ALSA underrun → USB hang → reboot |
| Unbounded queue without backpressure | Silent frame accumulation until cascade |
