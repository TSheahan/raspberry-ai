# Architecture Decisions

Key design choices for the two-process forked assistant, with rationale rooted in step 7 findings.

## Why Two Processes

The single-process Pipecat pipeline (steps 4–7) proved that every pipeline component works individually. The blocking issue is their interaction during and after the cognitive loop: CPU contention between ONNX inference and the Claude subprocess, combined with unbounded Pipecat queues and a shutdown race in PortAudio's C layer.

Process isolation resolves all three simultaneously:

| Single-process problem | Two-process resolution |
|---|---|
| CPU contention: Claude competes with ONNX inference | Processes on separate cores, no competition |
| Queue growth: frames accumulate during cognitive loop | Ring buffer at constant rate, no unbounded queue |
| USB audio starvation from CPU load | Recorder's dedicated core gives uninterrupted USB attention |
| Shutdown race: PyAudio callback vs asyncio teardown | Recorder tears down independently; killed child doesn't reboot Pi |

## Why SharedMemory + Pipe (not sockets, not files)

**SharedMemory (`multiprocessing.shared_memory`):**
- Standard library, Python 3.8+. Uses `/dev/shm` (tmpfs) on Linux.
- Single-writer/single-reader ring buffer needs no locks.
- Write cursor as monotonic uint64, coherent via aligned access on ARM64.
- 512KB ring ≈ 16s lookback at 16kHz int16 mono.
- Audio frames (640 bytes per 20ms) written by memcpy, sub-microsecond.

**Pipe (`multiprocessing.Pipe()`):**
- Unix domain socket pair with pickle serialization.
- Message-oriented (each `send()`/`recv()` is one complete dict).
- Sub-millisecond latency, kernel-buffered (~64KB).
- Selectable fd for event loop integration.

**Design principle:** Separate data plane from control plane. Audio data (high volume, continuous) flows through shared memory. Control signals (low volume, sporadic) flow through the pipe. Mixing them in a single channel (as Pipecat's frame stream does internally) creates the access-pattern conflict that caused the original queue accumulation.

## Why VAD as Sensor, Not Gate

The recorder child reports observations: wake word detected, speech started, speech stopped. The master decides what those observations mean for the current interaction mode.

This decouples audio sensing from consumption policy:
- **Quick command mode:** VAD_STOPPED → "utterance complete, batch-transcribe"
- **Dictation mode:** ignore VAD_STOPPED unless silence exceeds N seconds, keep streaming
- **Future multi-turn:** use VAD_STOPPED to segment turns but don't stop the stream

The alternative (the recorder child interpreting VAD events and deciding when to stop) locks the interaction model into the recorder's logic.

## Why PipelineState Object (from v11)

Both processes use a centralized state object that owns all shared mutable state, exposes read-only properties, and executes side-effects on phase transitions. Processors hold only a reference to the state object, never to each other.

**Before (v10):** Cross-reference graph between processors. State mutations scattered across three classes. Weakref wiring done manually in `main()`. Adding a new state-dependent behavior required touching multiple classes.

**After (v11):** Hub-and-spoke. All transitions go through `state.set_phase()`. Side-effects (stream pause, ONNX reset, counter reset) are centralized. Adding new behavior means adding one method to the state object.

This pattern maps directly to the two-process design: the recorder child has its own `RecorderState` (DORMANT → WAKE_LISTEN → CAPTURE), and the master will have a `MasterState` with its own phase model.

## Why Core Pinning

Pi 4 has 4 ARM Cortex-A72 cores. The recorder child is pinned to core 0 via `os.sched_setaffinity()` immediately after fork. The master uses cores 1–3.

Pinning prevents the OS scheduler from migrating the recorder process during a latency-sensitive audio callback. USB isochronous transfers are timing-sensitive — a missed deadline at the host controller level can cascade into buffer underruns.

## ReSpeaker Audio Configuration — Resolved (P-1 2026-04-02)

**Status: closed. 1-ch at 16kHz is confirmed correct and is the only working configuration.**

**P-1 findings (`test/smoke_respeaker_channels.py` run on Pi 2026-04-02):**

- Device: `seeed-4mic-voicecard: bcm2835-i2s-ac10x-codec0 (hw:3,0)`, 4 max input channels, 44100 Hz native default
- 1-ch @ 16kHz: opens OK, delivers real audio (plausible noise floor samples)
- 2-ch @ 16kHz: opens without error, correct byte count, but delivers silence even with sound present
- 4-ch @ 16kHz: same — opens without error, silence only

The seeed ALSA driver accepts 2-ch and 4-ch opens at 16kHz silently but only activates real capture on the 1-ch path. The large values seen in the initial 2-ch read were a stream initialization artifact (stale DMA buffer); confirmed silence on re-run with audio present.

The driver performs sample-rate conversion from the 44100 Hz hardware default to 16kHz on the 1-ch path. This is transparent to the application and confirmed working.

**Consequences:**
- `LocalAudioTransport` with no explicit `channels=` (Pipecat default = 1) is on the only valid path
- All ring buffer, OWW, and Silero math assuming 16kHz int16 mono is correct
- VAD sensitivity issues are not channel-packing symptoms — investigate Silero params/thresholds
- **P-2 (beamform shim) is cancelled.** Software beam-forming requires working 4-ch capture, which is not available at 16kHz in this driver. Individual mic channels are inaccessible without opening at 44100 Hz and doing manual SRC — not warranted unless detection quality proves insufficient.

## Why Recorder Is Capture-Only

The recorder child owns the microphone and nothing else. Playback (TTS) belongs to the master or a future separate process. This keeps the child simple and aligned with the ReSpeaker hat's input-focused design. It also avoids bidirectional audio I/O races in the same process.
