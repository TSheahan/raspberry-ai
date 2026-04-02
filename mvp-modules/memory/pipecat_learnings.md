# Pipecat Learnings

Distilled from 30+ iterations across step 6 and step 7 (2026-03-30 through 2026-04-01). These are proven rules, not suggestions — each was validated by isolating a specific failure mode on Pi 4 hardware.

## The process_frame Rule

Every `FrameProcessor` subclass **must** override `process_frame` and call both `super().process_frame()` and `push_frame()`:

```python
async def process_frame(self, frame: Frame, direction: FrameDirection):
    await super().process_frame(frame, direction)
    # ... frame-specific logic ...
    await self.push_frame(frame, direction)
```

**Why:** The base class `process_frame()` performs internal lifecycle bookkeeping (`__started` on `StartFrame`, `_cancelling` on `CancelFrame`) but **never calls `push_frame()`**. A subclass without this override silently swallows all frames — `StartFrame` never propagates (pipeline never becomes ready) and `CancelFrame` never propagates (Ctrl+C hangs for ~20s then requires SIGTERM).

**How this was missed:** The override looks like a no-op pass-through for processors that only act on specific frame types. During cleanup, it was deleted as dead code. This is the most dangerous Pipecat pitfall — the pipeline appears to start (logs show linking) but is silently broken.

**Source:** Step 6, incremental isolation v8a/v8b (single-variable confirmation).

## PipelineRunner Signal Handling

Let `PipelineRunner` handle SIGINT. Do not:
- Install custom signal handlers
- Pass `handle_sigint=False`
- Wrap `runner.run()` in `try/except KeyboardInterrupt`

Pipecat's built-in handler queues a `CancelFrame` that propagates through the pipeline cleanly. This is the intended shutdown path.

## Transport Cleanup

Do not manually call `transport.cleanup()`. `CancelFrame` propagation triggers transport cleanup automatically. Manual cleanup in `finally` blocks double-cleans and races with Pipecat's internal teardown.

## Post-Pipeline Work

Code that should run after pipeline shutdown goes after `await runner.run(task)`. This call blocks until the pipeline finishes (including clean `CancelFrame` propagation). Code after it runs in a fully shut-down pipeline — the correct place for final transcription, result logging, etc.

## RTVIProcessor

`PipelineTask` auto-injects `RTVIProcessor` by default (`enable_rtvi=True`). It is harmless — `CancelFrame` propagates through it in ~2ms. No need to disable unless there's a specific reason. `PipelineTask(pipeline, enable_rtvi=False)` is available if needed.

## Frame Type Routing in Custom Processors

When implementing gated processors (processors that conditionally process certain frames), ensure lifecycle frames (`StartFrame`, `EndFrame`, `CancelFrame`, `SystemFrame`) always reach internal controllers. Use the audio-only gating pattern:

```python
async def process_frame(self, frame, direction):
    await super().process_frame(frame, direction)
    await self.push_frame(frame, direction)  # always forward downstream

    if isinstance(frame, (AudioRawFrame, InputAudioRawFrame)):
        if should_process_audio:
            await self._controller.process_frame(frame)
    else:
        await self._controller.process_frame(frame)  # lifecycle events always reach controller
```

Routing `CancelFrame` into a controller that doesn't expect it can crash on exit. Make conditionals explicitly exhaustive.

## VADController Event Handlers

`on_speech_started` and `on_speech_stopped` are the **functional emission mechanism** for VAD, not optional observability hooks. Without `on_speech_stopped` calling `broadcast_frame(VADUserStoppedSpeakingFrame, ...)`, the downstream capturer never sees the trigger. The pipeline appears functional (wake word fires, audio captures) until you notice the cognitive loop never starts.

Diagnostic prints in these handlers are essential — silent VAD failure is indistinguishable from "nobody is speaking."

## Attribute Initialization Ordering

Any instance attribute referenced by an event handler must be initialized in `__init__` **before** handler registration. An `AttributeError` in an async handler raises but doesn't propagate visibly — the handler silently dies and the component appears to work but never responds to events.

## InputAudioRawFrame Is a SystemFrame

`InputAudioRawFrame` inherits from both `SystemFrame` and `AudioRawFrame`. This has a critical routing implication: each `FrameProcessor` has two internal tasks — `__input_frame_task_handler` (system frames) and `__process_frame_task_handler` (data frames). System frames are processed **inline in the input task** via `await self.__process_frame(...)` — they do NOT pass through `__process_queue`.

This means when a processor's `process_frame` calls a synchronous blocking function (e.g., ONNX `model.predict()`), the entire event loop is blocked. No other processor task can run. The block duration is the full wall-clock time of the synchronous call.

**Implication for instrumentation:** A bookend entry/exit probe pair correctly captures the full blocking cost of all intermediate processors, because the event loop is single-threaded and system frames are processed serially.

**Implication for performance:** Any synchronous blocking work in a processor's `process_frame` (ONNX inference, heavy numpy, file I/O) blocks the entire pipeline for that duration. `asyncio.to_thread()` can move such work off the event loop if the underlying C extension releases the GIL.

**Source:** Confirmed via pipecat 0.0.108 source inspection (`processors/frame_processor.py`, `transports/base_input.py`, `frames/frames.py`), 2026-04-02.

## Pipecat API Version Note

These learnings apply to `pipecat-ai` 0.0.108. The framework's internal API patterns (especially around `process_frame` propagation) may change in future versions.
