# Queue Depth Monitor — Design Analysis

*2026-04-03 — analysis of approach for always-on queue depth tripwire*

## Context

Duty cycle instrumentation (`DutyCycleCollector`) accumulates statistics that gauge the
system's tendency towards buffer overrun, but it's retrospective. More crashes are occurring
and the look-back data doesn't reveal *why* — only that things were generally OK across
100-frame windows.

The goal: a tripwire that alarms immediately when the audio input queue is accumulating,
since that condition signals degradation that could crash the Pi.

## 1. The Pressure Point: `_audio_in_queue`

`BaseInputTransport._create_audio_task()` creates an **unbounded `asyncio.Queue()`**.
PyAudio's callback runs in a dedicated PortAudio thread and enqueues via
`asyncio.run_coroutine_threadsafe(self.push_audio_frame(frame), self.get_event_loop())`.

The `_audio_task_handler` coroutine dequeues and pushes downstream:

```python
# base_input.py — one-at-a-time drain loop
while True:
    frame = await asyncio.wait_for(self._audio_in_queue.get(), timeout=0.5)
    # ... optional filter/VAD (deprecated path, not used by us) ...
    if self._params.audio_in_passthrough:
        await self.push_frame(frame)
    self._audio_in_queue.task_done()
```

`InputAudioRawFrame` inherits from `SystemFrame` — system frames are processed inline by
each processor's input task (not deferred to the process queue). Each processor's
`__input_frame_task_handler` directly awaits `process_frame()` for audio frames, one at
a time.

**Queue depth > 0 means the event loop is falling behind production.** Because the queue
is unbounded, there is no back-pressure — PyAudio keeps producing regardless of backlog
depth.

## 2. Current Observability Gap

`DutyCycleCollector` tracks queue depth but with three limitations:

1. **Retrospective only** — records `_window_max_qdepth` across a 100-frame window (~2s),
   prints in periodic summary. If the system crashes mid-window, the reading is lost.
2. **Gated to `ENABLE_DUTY_CYCLE=1`** — zero visibility in production runs without the flag.
3. **No alarm, no reaction** — a depth spike to 20 is silently folded into `q_max=20`
   and printed 60 frames later.

## 3. QueueDepthMonitor — Proposed Wrapper

A standalone class whose sole concern is queue depth observation and alarming:

```python
class QueueDepthMonitor:
    """Always-on tripwire for audio input queue backlog."""

    ALARM_THRESHOLD = 2    # immediate alarm when depth exceeds this
    # Future: CRITICAL_THRESHOLD for survival reactions

    def __init__(self, transport_input):
        self._transport_input = transport_input
        self._max_depth_seen = 0
        self._alarm_count = 0
        self._consecutive_alarms = 0

    def check(self) -> int:
        """Called once per audio frame. Returns depth, alarms if threshold exceeded."""
        qd = self._read_depth()
        if qd > self._max_depth_seen:
            self._max_depth_seen = qd
        if qd > self.ALARM_THRESHOLD:
            self._alarm_count += 1
            self._consecutive_alarms += 1
            print(f"  [QDEPTH ALARM] depth={qd} consecutive={self._consecutive_alarms}")
        else:
            self._consecutive_alarms = 0
        return qd

    def _read_depth(self) -> int:
        t = self._transport_input
        if t and hasattr(t, '_audio_in_queue'):
            return t._audio_in_queue.qsize()
        return -1
```

**Lifecycle**: Created unconditionally in `recorder_child_main()` — not gated to
`ENABLE_DUTY_CYCLE`. Takes the same `transport_input` reference that `DutyCycleCollector`
uses today. Lightweight: one `qsize()` call and an integer comparison per frame.

**Relationship to DutyCycleCollector**: The collector's `_queue_depth()` method and
`_window_max_qdepth` tracking delegate to the monitor. The collector's constructor takes
`Optional[QueueDepthMonitor]`; if present, it reads from the monitor's state for periodic
reporting instead of probing the queue directly.

## 4. Dual Integration Path

The monitor must observe every audio frame regardless of ENABLE_DUTY_CYCLE:

**When `ENABLE_DUTY_CYCLE=1`**: `DutyCycleEntry` is in the pipeline and already touches
every audio frame. It takes the monitor in its constructor and calls `monitor.check()`
alongside `collector.stamp_entry()`.

**When `ENABLE_DUTY_CYCLE=0`**: `DutyCycleEntry` is not in the pipeline. `GatedVADProcessor`
is the first processor to see audio frames. It takes an optional monitor and calls `check()`.

**In `recorder_child_main()`**: The monitor is always created. It's passed to `DutyCycleEntry`
if that exists, or to `GatedVADProcessor` if not. Exclusive-or integration — exactly one
processor calls `monitor.check()` per frame. No double-counting, no gaps.

## 5. Suppression Analysis

Three options for survival reactions when the queue is accumulating dangerously.

### Option A: Suppress `push_frame` at the monitoring point

The monitoring processor skips `await self.push_frame(frame, direction)` when depth
exceeds a critical threshold.

In the current pipeline order (`input → [DutyCycleEntry] → vad → oww → audio_writer`):
- **Audio is NOT written to the ring buffer** — STT gets incomplete audio
- **Wake word detection is skipped** — might miss "hey Jarvis"
- **VAD inference is skipped** — might miss speech boundaries

Drains the queue effectively (~microseconds per suppressed frame vs ~12ms normal), but
the trade-off is bad: you survive at the cost of losing the audio you're recording.

### Option B: Reorder pipeline, suppress after audio_writer

Move `audio_writer` to second position:

```
Current:  input → [DutyCycleEntry] → vad → oww → audio_writer → [DutyCycleExit]
Proposed: input → [DutyCycleEntry] → audio_writer → vad → oww → [DutyCycleExit]
```

Suppress `push_frame` inside `audio_writer` when the monitor signals pressure. Audio still
gets written (audio_writer runs before suppression), only VAD/OWW inference is starved.

Strictly better than Option A — ring buffer writes are preserved. But requires a pipeline
restructure, and suppression logic in AudioShmRingWriteProcessor is an awkward fit for that class's
responsibilities.

### Option C: Signal processors to skip inference (recommended)

The monitor exposes a pressure level. Each processor consults the monitor and skips its
expensive work when pressure is high, but **always pushes the frame downstream**:

```python
# In QueueDepthMonitor
@property
def pressure(self) -> int:
    """0=normal, 1=elevated, 2=critical"""
    ...

# In OpenWakeWordProcessor.process_frame — skip predict
if self._monitor and self._monitor.pressure >= 2:
    pass  # skip predict, push frame normally

# In GatedVADProcessor.process_frame — skip Silero inference
if self._monitor and self._monitor.pressure >= 2:
    self.state.inc_vad_frames()  # count but don't infer
```

**Consequences**:
- All frames flow through the entire pipeline — no dropped frames, no state confusion
- Ring buffer writes unaffected — audio_writer sees every frame
- OWW predict skipped under pressure — no to_thread + ONNX work
- Silero inference skipped under pressure — no synchronous event loop blocking
- Per-frame pipeline cost drops to ~1ms — queue drains rapidly
- No pipeline reorder needed

**Trade-offs**: During a pressure episode, wake words and speech boundaries are missed.
But if the system is overloaded enough to trigger this, it would crash otherwise. Missing
a wake word is strictly better than rebooting the Pi.

## 6. Recommended Approach

**Option C (inference skip) is the right approach.**

1. No frame dropping — ring buffer intact, no state confusion
2. Targets the actual bottleneck — ONNX inference is the expensive per-frame cost
3. No pipeline restructuring required
4. Graceful degradation: "recording but temporarily deaf" → "fully working" once pressure resolves

**Threshold design (two tiers)**:
- **Alarm at depth > 2**: diagnostic — log immediately. No behavioral change. This is the
  post-mortem trail for "why did it crash?"
- **Inference skip at depth > N (suggest 5–8, tunable)**: survival — shed ONNX load. Needs
  Pi tuning to find the right trigger.

**Staged implementation**:
1. `QueueDepthMonitor` class + alarm logic (pure diagnostics, zero risk)
2. Wire into `DutyCycleEntry` / `GatedVADProcessor` (dual path)
3. Refactor `DutyCycleCollector` to delegate queue depth to monitor
4. Add pressure-level API + inference skip in OWW and VAD (Phase 2)

**Detection latency caveat**: The monitor runs inside the pipeline, so it can only check
when the event loop yields to a processor's input task. If Silero blocks the event loop,
the monitor can't observe during that window. It sees the spike on the next frame. This is
inherent to in-process monitoring and is acceptable — the alarm fires within one frame
period of the blockage resolving.
