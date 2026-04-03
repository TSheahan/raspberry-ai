# Interface Specification: Recorder Child ↔ Master

**Date:** 2026-04-01
**Status:** Design spec
**Parent:** `forked_assistant/architecture.md`

---

## Overview

Two communication channels cross the process boundary:

1. **Ring buffer** (shared memory) — continuous audio data, recorder→master only
2. **Command pipe** (Unix domain socket) — bidirectional control signals

These channels are independent. The ring buffer is always being written while the recorder child is active. The pipe carries infrequent, immediate-delivery messages.

---

## 1. Ring Buffer

### Memory layout

```
SharedMemory name: "recorder_audio"
Total size: HEADER_SIZE + RING_SIZE

HEADER (64 bytes, offset 0):
  bytes 0–7:   write_pos    uint64 LE   monotonically increasing byte offset
  bytes 8–15:  sample_rate  uint64 LE   sample rate in Hz (16000)
  bytes 16–17: channels     uint16 LE   channel count (1)
  bytes 18–19: sample_width uint16 LE   bytes per sample (2, for int16)
  bytes 20–23: frame_size   uint32 LE   bytes per frame as written (640 = 20ms @ 16kHz int16)
  bytes 24–63: reserved     zero-filled

RING REGION (offset 64, length RING_SIZE):
  Circular buffer of raw int16 PCM samples.
```

### Constants

```python
HEADER_SIZE = 64
RING_SIZE   = 524288    # 512KB = ~16.4 seconds at 32KB/s (16kHz int16 mono)
SHM_SIZE    = HEADER_SIZE + RING_SIZE
SHM_NAME    = "recorder_audio"
```

### Write protocol (recorder child)

On each PyAudio callback (every 20ms, 640 bytes):

```python
offset = write_pos % RING_SIZE
# Handle wrap-around
if offset + frame_size <= RING_SIZE:
    shm.buf[HEADER_SIZE + offset : HEADER_SIZE + offset + frame_size] = frame_bytes
else:
    first = RING_SIZE - offset
    shm.buf[HEADER_SIZE + offset : HEADER_SIZE + RING_SIZE] = frame_bytes[:first]
    shm.buf[HEADER_SIZE : HEADER_SIZE + (frame_size - first)] = frame_bytes[first:]

# Advance write_pos (single atomic-width store on ARM64)
struct.pack_into('<Q', shm.buf, 0, write_pos + frame_size)
```

The recorder child writes in **all active states** (WAKE_LISTEN and CAPTURE). In DORMANT, writes stop.

### Read protocol (master)

The master maintains its own `read_pos`. To read audio between two positions:

```python
def read_ring(shm, start_pos, end_pos):
    """Read audio bytes from ring buffer between two write_pos values."""
    length = end_pos - start_pos
    if length <= 0 or length > RING_SIZE:
        return b''  # nothing to read, or data has been overwritten
    
    start_offset = start_pos % RING_SIZE
    if start_offset + length <= RING_SIZE:
        return bytes(shm.buf[HEADER_SIZE + start_offset : HEADER_SIZE + start_offset + length])
    else:
        first = RING_SIZE - start_offset
        part1 = bytes(shm.buf[HEADER_SIZE + start_offset : HEADER_SIZE + RING_SIZE])
        part2 = bytes(shm.buf[HEADER_SIZE : HEADER_SIZE + (length - first)])
        return part1 + part2
```

**Staleness check:** if `current_write_pos - read_pos > RING_SIZE`, the data at `read_pos` has been overwritten. The master should detect this and skip ahead.

### Streaming read (future dictation mode)

For streaming STT, the master tails the ring buffer:

```python
read_pos = start_pos  # from WAKE_DETECTED or VAD_STARTED signal
while streaming:
    current_write_pos = struct.unpack_from('<Q', shm.buf, 0)[0]
    available = current_write_pos - read_pos
    if available > 0:
        chunk = read_ring(shm, read_pos, current_write_pos)
        send_to_streaming_stt(chunk)
        read_pos = current_write_pos
    await asyncio.sleep(0.04)  # or wake on pipe signal
```

The consumption policy (when to stop streaming, how to handle VAD signals) is entirely the master's decision.

---

## 2. Signal Protocol

### Transport

`multiprocessing.Pipe(duplex=True)` — creates a pair of `Connection` objects. Each side calls `send(dict)` / `recv()`. Messages are pickled Python dicts.

### Message format

Every message is a dict with a `"cmd"` key and optional payload keys:

```python
{"cmd": "COMMAND_NAME", ...payload}
```

### Master → Recorder commands

These control the recorder child's operating state.

| Command | Payload | Effect |
|---------|---------|--------|
| `SET_DORMANT` | — | Stop listening. Stream stops. Ring buffer writes stop. Silero and OWW idle. |
| `SET_WAKE_LISTEN` | — | Listen with wake word detection. Stream active. Ring buffer writes active. OWW runs. Silero does not run. |
| `SET_CAPTURE` | — | Listen with VAD. Stream active. Ring buffer writes active. Silero runs. OWW does not run. |
| `SHUTDOWN` | — | Clean exit. Stop stream, close PyAudio, release shared memory, exit process. |

Note: `SET_CAPTURE` is sent by the master in response to receiving `WAKE_DETECTED`. This gives the master control over whether to honour the wake event (e.g., it may ignore if already processing).

### Recorder → Master signals

These report events observed by the recorder child.

| Signal | Payload | Meaning |
|--------|---------|---------|
| `WAKE_DETECTED` | `{"write_pos": int, "score": float, "keyword": str}` | Wake word fired. `write_pos` is the ring buffer position at detection time. |
| `VAD_STARTED` | `{"write_pos": int}` | Speech onset detected by Silero. |
| `VAD_STOPPED` | `{"write_pos": int}` | Speech offset detected by Silero. |
| `STATE_CHANGED` | `{"state": str}` | Acknowledgement that the recorder has entered the requested state. |
| `ERROR` | `{"msg": str}` | Something went wrong in the recorder child. |
| `READY` | — | Recorder child has initialized and is ready to accept commands. |
| `SHUTDOWN_COMMENCED` | — | Child has begun safe teardown (stream stopping, pipeline draining). Sent once, on first shutdown trigger. |
| `SHUTDOWN_FINISHED` | — | Child teardown complete. Process is about to exit. Master may proceed with cleanup. |

### Typical turn sequence

```
Master                              Recorder Child
──────                              ──────────────
                                    (starts in DORMANT)
                                    ──→ READY
SET_WAKE_LISTEN ──────────────────→
                                    ←── STATE_CHANGED {state: "wake_listen"}
                                    (OWW running, ring buffer writing)
                                    ...time passes...
                                    ←── WAKE_DETECTED {write_pos: 34560, score: 0.72}
SET_CAPTURE ──────────────────────→
                                    ←── STATE_CHANGED {state: "capture"}
                                    (Silero running, OWW stopped, ring writing)
                                    ←── VAD_STARTED {write_pos: 35200}
                                    ←── VAD_STOPPED {write_pos: 72320}
(reads ring[35200:72320])
(runs STT + Claude)
SET_WAKE_LISTEN ──────────────────→
                                    ←── STATE_CHANGED {state: "wake_listen"}
                                    (OWW reset + running, Silero stopped)
...next turn...
```

### Master-side consumption logic (batch mode)

```python
wake_pos = None
vad_start_pos = None

while True:
    msg = pipe.recv()
    
    if msg["cmd"] == "WAKE_DETECTED":
        wake_pos = msg["write_pos"]
        pipe.send({"cmd": "SET_CAPTURE"})
    
    elif msg["cmd"] == "VAD_STARTED":
        vad_start_pos = msg["write_pos"]
    
    elif msg["cmd"] == "VAD_STOPPED":
        # Read captured audio from ring buffer
        start = vad_start_pos or wake_pos
        audio_bytes = read_ring(shm, start, msg["write_pos"])
        
        # Return to wake listening while cognitive loop runs
        pipe.send({"cmd": "SET_WAKE_LISTEN"})
        
        # Run cognitive loop (STT + Claude)
        await process_utterance(audio_bytes)
```

---

## 3. Process Lifecycle

### Startup sequence

```
1. Master creates SharedMemory (SHM_NAME, SHM_SIZE)
2. Master creates Pipe (duplex=True)
3. Master spawns recorder child via multiprocessing.Process,
   passing: child pipe end, SHM_NAME
4. Recorder child pins to core 0 via os.sched_setaffinity(0, {0})
5. Recorder child opens SharedMemory by name
6. Recorder child initializes Pipecat pipeline, PyAudio, OWW, Silero
7. Recorder child sends READY
8. Master sends SET_WAKE_LISTEN
9. Normal operation begins
```

### Shutdown sequence

Shutdown may be triggered by either side:

- **External ^C (SIGINT):** Delivered to the process group. Both master and child receive it. The child initiates safe teardown from its SIGINT handler. The master catches KeyboardInterrupt and enters the shutdown wait path.
- **Master-initiated:** Master sends SHUTDOWN over pipe (e.g., future programmatic shutdown). Child receives it via command_listener and initiates safe teardown.
- **Child-initiated SIGTERM:** Master's escalation path. Child receives SIGTERM and initiates safe teardown.

Regardless of trigger, the child runs the same once-only teardown sequence:

```
Child teardown (once-only, first trigger wins):
  1. Send SHUTDOWN_COMMENCED to master
  2. set_phase("dormant") — stops PyAudio stream, 100ms settle
  3. Cancel pipeline task — CancelFrame propagates, pipeline drains
  4. Print diagnostics (queue depth summary, duty cycle if enabled)
  5. Send SHUTDOWN_FINISHED to master
  6. Close shared memory, exit process

Master wait path (runs on all exit paths via finally):
  1. Send SHUTDOWN command on pipe (idempotent — child may already be tearing down)
  2. Drain pipe, waiting for SHUTDOWN_FINISHED (5s deadline)
  3. child.join(timeout=2)
  4. If child hasn't exited: child.terminate() (SIGTERM → triggers same safe teardown)
  5. child.join(timeout=2)
  6. If still alive: child.kill() (SIGKILL)
  7. Unlink SharedMemory
```

**Critical invariant:** The master MUST wait for SHUTDOWN_FINISHED (or pipe close / timeout) before proceeding with SharedMemory cleanup. The child's stream-stop-first ordering is what prevents the PortAudio/USB race condition that causes Pi reboots.

### Crash recovery

If the recorder child dies unexpectedly:
- Master detects via `child.is_alive()` check or broken pipe (`EOFError` on `recv()`)
- Master unlinks and recreates SharedMemory
- Master spawns a new recorder child
- No Pi reboot — a dead child process just releases its resources

---

## 4. Audio Format Constants

These are fixed for the ReSpeaker hat at the current configuration:

```python
SAMPLE_RATE    = 16000   # Hz
CHANNELS       = 1       # mono
SAMPLE_WIDTH   = 2       # bytes (int16)
FRAME_DURATION = 0.020   # seconds (20ms)
FRAME_SAMPLES  = 320     # samples per frame
FRAME_BYTES    = 640     # bytes per frame
```
