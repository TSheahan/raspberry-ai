# Python-ALSA Interface Brief — raspberry-ai

*2026-04-03 — Roadmap document for Workstream 2: deep driver/ALSA/Python interface analysis*

---

## 1. Purpose and scope

This is one of two roadmap documents prepared before the Workstream 2 analysis agent is dispatched. The companion document lives in the `seeed-voicecard` project and covers the driver side. Together they orient the agent so it can allocate its context window efficiently across both projects. This document is a guide, not a curated report — the agent decides what to read and how deeply. Its value is in signposting what the raspberry-ai project contains, what carries the most signal for the driver analysis question, and what can be safely deferred or skipped. The driver analysis question is: what in the `seeed-voicecard` and `ac108` kernel driver teardown path causes a Pi reboot on stream close?

---

## 2. Hardware and ALSA path (confirmed)

- **Device:** `seeed-4mic-voicecard: bcm2835-i2s-ac10x-codec0 (hw:3,0)`, opened by PyAudio as device index `1`
- **Transport:** I2S via GPIO header — no USB anywhere in the audio path
- **Codec:** AC108 ADC chip on the ReSpeaker 4-Mic HAT, controlled via I2C from the driver
- **ALSA plug:** the `ac108` named PCM (configured by `/etc/asound.conf`) performs S32_LE → S16_LE conversion; 1-ch 16 kHz mono is the confirmed working format for all pipeline stages
- **Kernel:** `6.12.75+rpt-rpi-v8` (aarch64, Raspberry Pi OS Trixie)
- **Driver:** HinTak/seeed-voicecard community fork, branch `v6.12` (now also tracked as `TSheahan/seeed-voicecard`, branch `ac108-shutdown-fix`)

---

## 3. PyAudio stream configuration

The recorder child opens one input stream, no output:

```python
LocalAudioTransport(LocalAudioTransportParams(
    audio_in_enabled=True,
    audio_in_device_index=1,   # ReSpeaker via ac108 plug
    audio_out_enabled=False,
))
```

- **Callback mode** (not blocking mode) — `_audio_in_callback` runs in a dedicated PortAudio thread, independent of the asyncio event loop
- The callback is monkey-patched before the stream opens (see section 5)
- Audio format: 16 kHz, 1-ch, int16, 320 samples (640 bytes) per 20 ms frame

---

## 4. Stream lifecycle — start, run, stop

### Start

`_start_stream()` in `recorder_state.py:200–209`:
- Clears `_stop_producing = False`
- Calls `_in_stream.start_stream()` → `Pa_StartStream()` → ALSA PCM open/prepare/start → `TRIGGER_START` on codec

### Run

- Callback returns `pyaudio.paContinue`; each invocation pushes one `InputAudioRawFrame` to the asyncio event loop via `run_coroutine_threadsafe`
- ONNX inference (OWW wake word, Silero VAD) runs in thread pool executors — neither blocks the event loop during inference
- The ALSA driver ring buffer is consumed at production rate regardless of event loop state (callback-mode isolation)

### Stop — the driver-critical path

The stop path uses a layered defence designed to avoid calling `Pa_StopStream()` / `Pa_CloseStream()` from outside the PortAudio callback thread. Each layer is described in section 5. The invariant being enforced: PortAudio must stop the stream from *within* the callback thread via `paComplete`, not from an external thread via `Pa_StopStream`.

After all Python-level teardown completes, `os._exit(0)` fires (section 6). The kernel then closes all open file descriptors, including the ALSA PCM FD. This triggers `snd_pcm_release()` → driver teardown regardless of what Python has done. This is the path that crashes.

---

## 5. Monkey-patches applied at startup

All four patches are applied in `recorder_child.py:651–719`, before `Pipeline(processors)` and `runner.run(task)`. The stream is not yet open when the patches are installed.

### Patch 1 — guarded callback (`recorder_child.py:657–664`)

Wraps `_audio_in_callback` to check a `_stop_producing` flag:

```python
input_transport._stop_producing = False
_real_callback = input_transport._audio_in_callback

def _guarded_callback(in_data, frame_count, time_info, status):
    if input_transport._stop_producing:
        return (None, pyaudio.paComplete)
    return _real_callback(in_data, frame_count, time_info, status)

input_transport._audio_in_callback = _guarded_callback
```

When `_stop_producing = True`, the callback returns `paComplete` on its next invocation. PortAudio then stops the stream from within the callback thread — the safe path. A 100ms settle follows to confirm the stream has stopped.

### Patch 2 — cancel with flag stop (`recorder_child.py:691–702`)

Wraps `input_transport.cancel` (called when `CancelFrame` propagates through the pipeline):

```python
async def cancel_with_flag_stop(frame):
    if not input_transport._stop_producing:
        input_transport._stop_producing = True
        await asyncio.sleep(0.1)
    await original_cancel(frame)

input_transport.cancel = cancel_with_flag_stop
```

Ensures `_stop_producing` is set (and 100ms elapses) before the original cancel proceeds. On the normal shutdown path, `_stop_producing` is already `True` from `_stop_stream()`, so the sleep is skipped.

### Patch 3 — safe cleanup (`recorder_child.py:712–719`)

Wraps `input_transport.cleanup` (called at end of pipeline teardown):

```python
async def _safe_cleanup():
    await _FP.cleanup(input_transport)   # base class lifecycle bookkeeping only
    input_transport._in_stream = None    # null out; do NOT call close()
```

Skips both `stop_stream()` and `close()` (`Pa_StopStream` / `Pa_CloseStream`). Sets `_in_stream = None` so nothing else tries to touch the stream handle. The kernel will release the PCM device on process exit.

### Patch 4 — os._exit(0) (`recorder_child.py:834`)

Not a monkey-patch on a Pipecat object, but functionally equivalent. After `asyncio.run()` returns (all Python cleanup complete), `os._exit(0)` is called from `recorder_child_entry`. This bypasses Python's normal shutdown including `PyAudio.__del__` → `Pa_Terminate()`, which would otherwise close all open PortAudio streams at the C level and re-trigger the same driver path.

---

## 6. Shutdown sequence end-to-end

On the proven two-phase shutdown path (confirmed clean at Python level, 2026-04-03 run 3):

```
1.  Master sends SHUTDOWN or SIGINT arrives
2.  _initiate_shutdown() fires (once-only guard)
3.  SHUTDOWN_COMMENCED sent to master
4.  state.set_phase("dormant") → _stop_stream():
      _stop_producing = True
      await asyncio.sleep(0.1)       ← 100ms settle
5.  task.cancel() → CancelFrame propagates through pipeline
6.  cancel_with_flag_stop: _stop_producing already True → 100ms → original_cancel(frame)
7.  Pipeline drains → _safe_cleanup(): _in_stream = None  (no Pa_StopStream, no Pa_CloseStream)
8.  asyncio.run() finally block:
      QDEPTH summary printed
      duty cycle summary printed
      SHUTDOWN_FINISHED sent to master
      shm.close()
9.  asyncio.run() returns
10. os._exit(0)  ← bypasses PyAudio.__del__ / Pa_Terminate()

--- kernel takes over ---

11. Kernel closes all open FDs on process exit, including the ALSA PCM FD
12. snd_pcm_release() → seeed-voicecard / ac108 driver teardown
    *** crash occurs here ***
```

Step 10 is verified: on a confirmed-clean run, `[child] SHUTDOWN_FINISHED sent` and `[child] exiting` both appear in the log, followed by `[master] done` and a clean shell prompt. The crash that remains occurs at step 12, below the Python layer.

---

## 7. The crash — confirmed facts only

- **Symptom:** `client_loop: send disconnect: Connection reset` (Pi reboots) on shutdown after ≥1 completed voice turn
- **Crash location:** kernel driver teardown path (`snd_pcm_release()` → `ac108_aif_shutdown`), not Python
- **`os._exit(0)` does not prevent it:** bypasses Python destructors but not kernel FD cleanup on process exit
- **paComplete pattern may still be beneficial:** it avoids triggering `TRIGGER_STOP` from outside the callback thread, which may interact poorly with the AC108 driver state machine — but it does not prevent `snd_pcm_release()` calling `shutdown`
- **Crash is exit-specific:** no crash observed during sustained recording; crash occurs only at stream close / process exit
- **Python-side ordering proven clean:** the two-phase shutdown protocol is confirmed stable (2026-04-03 run 3); the remaining crash is in the driver
- **Prior misattribution (now corrected):** earlier sessions attributed the crash to USB/xHCI faults. The ReSpeaker uses I2S, not USB. All references to "USB fault" or "xHCI" in older memory file content are wrong. `shutdown_and_buffer_patterns.md` Root Cause 5 documents this correction in full.

---

## 8. Project structure and reading guidance

### `forked_assistant/src/` — the five operative files

`recorder_child.py` is the central file for the driver interface question. It owns the PyAudio stream, all four monkey-patches, the processor pipeline, the shutdown sequence, and the process entry point. Everything else in `src/` is either context for understanding its behaviour, or belongs to the master process which has no ALSA interaction.

| File | Lines | Role | Reading guidance |
|---|---|---|---|
| `recorder_child.py` | 836 | Central. Transport init, all monkey-patches, processors (GatedVAD, OWW, AudioShmRingWriteProcessor), shutdown sequence, `os._exit(0)`. | Primary source. Driver-interface-relevant code is concentrated in lines 639–835 (transport setup through entry point). The processor classes (lines 340–600) are context for understanding what runs during the stream's lifetime. |
| `recorder_state.py` | 317 | State machine and stream lifecycle methods. Processors hold weakrefs into this object; it owns `_start_stream` / `_stop_stream`. | `_start_stream` and `_stop_stream` (lines 200–236) are directly relevant — they implement the paComplete flag mechanism. The rest is state machine wiring and OWW/Silero reset logic, useful for understanding phase transitions but not the driver interface directly. |
| `audio_shm_ring.py` | (see `assistant/`) | SharedMemory ring buffer for audio transfer to master. No ALSA interaction. | Read only if tracing `write_pos` signal payloads in the shutdown sequence matters for reconstructing the crash timeline. Otherwise not relevant. |
| `master.py` | 359 | Master process: pipe protocol, STT (Deepgram), Claude subprocess. No PyAudio, no ALSA. | Not relevant to driver analysis. Useful if understanding the master's role in the crash scenario (e.g., timing between SET_IDLE and Ctrl+C) adds context. |
| `log_config.py` | ~50 | Logging setup only. | Skip. |

### Memory files — `mvp-modules/memory/`

Three distilled-findings documents. High signal per line, but all carry known stale content from the USB misattribution period. Read critically.

- **`shutdown_and_buffer_patterns.md`** — highest relevance. Root Causes 3–5 cover the shutdown crash history, the paComplete/`os._exit` mitigation chain, the I2S misidentification and correction, and the current state of Python mitigations. Root Causes 1–2 are Python-layer buffer/CPU findings (resolved) and are not driver-relevant. The "Stream Lifecycle Anti-Patterns" table at the end is a concise reference for what was tried and why.

- **`architecture_decisions.md`** — "ReSpeaker Audio Configuration" (confirmed ALSA device path, S32_LE/S16_LE findings, PGA investigation), "Two-Phase Shutdown Protocol" (shutdown sequence rationale), and "Why Core Pinning and SCHED_FIFO" (I2S DMA timing concern) are all relevant. Sections on OWW duty cycle, ring buffer design, and VAD-as-sensor are not driver-relevant.

- **`pipecat_learnings.md`** — Pipecat API patterns from 30+ iterations (pipecat-ai 0.0.108). Not driver-relevant. Read only if tracing how Pipecat's frame routing (specifically `InputAudioRawFrame` as `SystemFrame`, processed inline) affects the call sequence reaching the ALSA layer.

### Archive and spec files

- **`forked_assistant/spec/`** — architecture, interface spec, recorder state spec, stub contracts, implementation framework. `implementation_framework.md` has the full EU-by-EU development history including all Pi run findings. Useful if the crash scenario needs more development context, but not necessary for driver analysis.

- **`forked_assistant/archive/alarming_queue_depths/`** — two docs from a mid-stage investigation into whether pipeline backpressure could crash the Pi via ALSA buffer overflow. Conclusion: it cannot (callback-mode decoupling confirmed). Read only if the driver analysis raises questions about whether the ALSA-side ring buffer overflowed contributing to the crash.

- **`mvp-modules/archive/step6/` and `step7/`** — single-process Pipecat pipeline iterations (13 versions), superseded by `forked_assistant`. Skip unless tracing the historical progression of the crash (Root Causes 3–4) is relevant.

- **`.venv/Lib/site-packages/`** — pinned copies of `pipecat-ai` 0.0.108 and related packages as installed on the Pi. The Pi does not execute from this path — present as reference source only. Useful for confirming Pipecat internals (e.g., what `LocalAudioInputTransport.cleanup()` calls by default before the monkey-patch overrides it, or how `Pa_StopStream` is invoked in the unpatched path).

---

## 9. Key questions for driver analysis

These are the specific unknowns that the driver source must resolve. They are stated as questions rather than conclusions because the answer in each case depends on reading driver code and kernel headers, not on what the Python side can observe.

1. **Teardown call sequence:** When the ALSA PCM FD closes (step 12 in section 6), which kernel path reaches `ac108_aif_shutdown`? Does `TRIGGER_STOP` always fire before `shutdown`, or can `snd_pcm_release()` call `shutdown` without a prior `TRIGGER_STOP` (e.g., when the stream was already stopped via paComplete)?

2. **paComplete and TRIGGER_STOP:** When PortAudio stops the stream internally via `paComplete`, does it generate a `snd_pcm_drop()` or `snd_pcm_drain()` call (or equivalent) that reaches `ac108_trigger(STOP)` before the FD is closed? If so: does `ac108_trigger(STOP)` and any deferred work it schedules (`work_cb_codec_clk`) complete cleanly before `snd_pcm_release()` calls `shutdown`?

3. **Sleeping in atomic context:** In `ac108_trigger` TRIGGER_START, `ac10x_read()` and `ac108_multi_update_bits()` are called inside `spin_lock_irqsave`. If these functions perform real I2C bus transactions (blocking), this is a sleeping-in-atomic-context kernel BUG that will panic. Does the regmap configuration for the AC108 use `REGCACHE_RBTREE` (or similar) such that reads are served from cache without I2C? Confirm from regmap init in `ac108.c` or `ac10x.h`.

4. **Workqueue race:** `seeed_voice_card_trigger` TRIGGER_STOP defers `ac108_set_clock(0, ...)` to a workqueue via `schedule_work(&work_codec_clk)` when called from interrupt context. Can this deferred work race with `ac108_aif_shutdown` — i.e., is it possible for the workqueue item to still be pending when `snd_pcm_release()` calls `shutdown`?

5. **Error handling in shutdown:** In `ac108_aif_shutdown`, the I2C writes (`MOD_CLK_EN=0`, `MOD_RST_CTRL=0`) are the last operations before the driver releases the stream. Are the return values of these writes checked? If the I2C bus hangs (e.g., due to a prior atomic-context violation), what does the kernel do — silent ignore, or kernel BUG / panic?
