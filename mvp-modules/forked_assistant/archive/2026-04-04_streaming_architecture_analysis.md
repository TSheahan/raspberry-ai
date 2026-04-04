# Streaming Architecture Analysis — EU-5 Design Brief
**Date:** 2026-04-04  
**Scope:** EU-5 (Streaming STT) and EU-6 (Streaming Claude) design decisions for the forked assistant  
**Prepared for:** Developer implementing EU-5 on Raspberry Pi 4

---

## 1. Executive Summary

Key recommendations for EU-5 and beyond:

- **Use the Mixed mode for EU-5.** Keep Silero VAD as the dispatch trigger (VAD_STOPPED → close WebSocket and process transcript). Open a live WebSocket on WAKE_DETECTED and tail the ring buffer. This eliminates the batch round-trip latency (currently ~1.8s STT) while preserving Silero as the utterance boundary oracle. This is the lowest-risk path that directly maps to the EU-5 spec.

- **Do not replace Silero with Deepgram endpointing as the primary dispatch trigger** for this architecture. Deepgram endpointing defaults to 10ms silence detection and is not tunable above a few hundred ms without degrading responsiveness. Long-pause dictation requires explicit `utterance_end_ms` which creates its own event-timing complexity — manageable, but adds state machine surface area with no gain when Silero already provides the pause-detection you own and can tune.

- **For dictation mode (20–30s pauses):** Increase `stop_secs` to 25–30s. This is a single parameter change, carries no EU-5 risk, and directly solves the stated problem. Pair it with a `FORCE_DISPATCH` pipe command for the optional 'finished' button.

- **The `dg_client.listen.live.v("1")` call pattern referenced in the EU-5 spec is outdated.** The current SDK (v6, last indexed March 2026) uses `deepgram.listen.v1.connect(...)` as a context manager. The old `live.v("1")` style was a v2/v3 API. See Section 2 for the current call pattern.

- **KeepAlive is mandatory during VAD gaps.** If Silero VAD is active and the user is silent for >10s, the WebSocket will time out with `NET-0001`. The ring tail thread must send `{"type": "KeepAlive"}` (as a text frame, not binary) every 3–5s when no audio bytes are being dispatched. Audio word timestamps are unaffected by KeepAlive.

- **Nova-3 streaming WER is 6.84% vs 5.26% for batch.** The quality gap is real (~1.6 percentage points WER). For this use case (conversational / dictation), the latency gains of streaming outweigh the marginal accuracy degradation. The gap primarily affects short utterances and numerics.

- **`claude -p` with `subprocess.run` has a known regression and buffering issue.** Use `--output-format stream-json` with `Popen` + line-buffered stdout for EU-6. Parse each newline-delimited JSON object and filter for `text_delta` events. The Claude Agent SDK Python transport already implements this pattern.

- **Pipecat 0.0.108 is current** (released 2026-03-27/28) and actively maintained. There is no EOL concern. The constraint in `AGENTS.md` (must override `process_frame`) remains in effect.

- **OpenWakeWord 0.4.0 is stuck.** v0.6.0 requires `tflite-runtime` wheels that are not available for ARM64/Python 3.11+. The project is correct to pin at 0.4.0. No viable upgrade path unless you containerize or pin Python ≤3.11 explicitly.

- **No Pi 4 ARM64 ONNX threading issues** are expected for EU-5. Silero ONNX on ARM runs at ~165× real-time. The existing non-overlapping phase design (OWW and Silero never concurrent) already prevents the primary risk.

---

## 2. Deepgram Live API — Current State and Parameters

### 2.1 API Surface (SDK v6, March 2026)

**[Research-confirmed]** The current Python SDK (v6, indexed 2026-03-13 on DeepWiki at commit `74f48c5`) has two live streaming APIs:

| API | Endpoint | Context Manager | Use Case |
|-----|----------|-----------------|----------|
| Listen v1 | `/v1/listen` | `deepgram.listen.v1.connect(...)` | General-purpose live transcription |
| Listen v2 | `/v2/listen` | `deepgram.listen.v2.connect(...)` | Conversational AI with EOT turn detection |

**The `dg_client.listen.live.v("1")` call pattern from the EU-5 spec is a v2/v3 SDK pattern and is no longer correct.** The current call is:

```python
with deepgram.listen.v1.connect(
    model="nova-3",
    encoding="linear16",
    sample_rate=16000,
    channels=1,
    language="en-US",
    smart_format=True,
    interim_results=True,
    utterance_end_ms="1000",   # optional; see §2.3
    vad_events=True,
    endpointing=300,           # optional; see §2.3
) as connection:
    connection.on(EventType.MESSAGE, on_message)
    connection.on(EventType.ERROR, on_error)
    connection.start_listening()  # blocking; run in thread
    connection.send_media(audio_bytes)
    connection.send_finalize()    # flush at end of utterance
    connection.send_close_stream()
```

Sources: [Deepgram Getting Started — Live Streaming](https://developers.deepgram.com/docs/live-streaming-audio), [DeepWiki Real-time Streaming (Listen v1 & v2)](https://deepwiki.com/deepgram/deepgram-python-sdk/3.2-real-time-streaming-(listen-v2))

**Listen v2** has contextual turn detection (`eot_threshold`, `eager_eot_threshold`, `eot_timeout_ms`) but **does not support `send_finalize()` or `send_keep_alive()`**. For the ring-tail architecture (where you control utterance dispatch externally via Silero), use **Listen v1**.

### 2.2 Response Events

Relevant message types received via `EventType.MESSAGE` callback:

| `message.type` | When fired | Key fields |
|----------------|------------|------------|
| `"Results"` | Every transcript result | `is_final`, `speech_final`, `channel.alternatives[0].transcript` |
| `"UtteranceEnd"` | When `utterance_end_ms` gap detected | `last_word_end` (float, seconds) |
| `"SpeechStarted"` | VAD onset detected (requires `vad_events=True`) | — |
| `"Metadata"` | After connection opens | session info |

For EU-5, accumulate `is_final=True` transcript segments and dispatch the full string when the WebSocket is closed (triggered by VAD_STOPPED). `speech_final=True` indicates Deepgram's own endpointing fired — useful as a secondary signal but should not replace the VAD_STOPPED trigger.

### 2.3 Endpointing Parameters

**[Research-confirmed]** From [Deepgram Endpointing docs](https://developers.deepgram.com/docs/endpointing) and [End of Speech Detection](https://developers.deepgram.com/docs/understanding-end-of-speech-detection):

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `endpointing` | int (ms) or `"false"` | 10ms | Silence duration triggering `speech_final=true`. Audio-VAD based. Can be set up to ~500ms usefully; above that, UtteranceEnd is preferable. |
| `utterance_end_ms` | int (ms), min 1000 | off | Word-timing gap analysis. Fires `UtteranceEnd` event. Requires `interim_results=True`. Word-timing based, so ignores background noise. Minimum useful value is 1000ms (Deepgram interim results are sent every ~1s). |
| `vad_events` | bool | false | Enables `SpeechStarted` events. Useful for barge-in detection but not required for EU-5. |
| `interim_results` | bool | false | Required if using `utterance_end_ms`. Also enables partial transcripts — useful for UI feedback. |

**Interaction with Silero:** Deepgram's own VAD (endpointing) and Silero VAD operate independently on different audio. Deepgram sees the audio bytes sent over the WebSocket; Silero runs locally on the ring buffer. They will not agree exactly on silence boundaries due to network buffering, but in the mixed-mode design Silero is authoritative and Deepgram endpointing is advisory.

### 2.4 Keep-Alive Requirements

**[Research-confirmed]** From [Audio Keep Alive docs](https://developers.deepgram.com/docs/audio-keep-alive):

- **10-second timeout:** If no audio bytes or KeepAlive messages are received within 10s, the connection closes with `NET-0001`.
- **KeepAlive interval:** Send every 3–5 seconds during silence.
- **Format:** JSON text frame (not binary): `{"type": "KeepAlive"}`. Sending as binary may fail.
- **SDK method:** `connection.send_keep_alive()` (v1 only; v2 does not have this method).
- **Word timestamps:** KeepAlive does not affect audio timestamps. After a silence gap with KeepAlives, audio resumes timestamp counting from where audio left off, not wall time.

**For dictation mode with 20–30s pauses:** The ring tail thread must run a KeepAlive timer alongside the audio polling loop. When the ring write_pos has not advanced (silence), send `send_keep_alive()` every 3–4s. When write_pos advances, send the audio bytes as normal.

### 2.5 Nova-3 Streaming vs Batch Quality

**[Research-confirmed]** From [Vapi blog: Nova-3 vs Nova-2](https://vapi.ai/blog/deepgram-nova-3-vs-nova-2) and [Deepgram STT changelog](https://developers.deepgram.com/changelog/speech-to-text-changelog/2025/2/12):

| Mode | WER | vs Nova-2 |
|------|-----|-----------|
| Batch (pre-recorded) | 5.26% | −47.4% from Nova-2 |
| Streaming (live) | 6.84% | −54.3% from Nova-2 streaming |

The streaming/batch WER gap (~1.6pp) is inherent to streaming models: the acoustic model has less right-context when finalizing words. For short utterances (<3s) the gap may be more pronounced. For conversational and dictation use cases on Pi, the latency benefit of streaming (~1.5–2s earlier first result) typically outweighs this accuracy delta. The current batch STT latency measured at ~1.82s (EU-4 run 3); streaming should deliver first results within 300–500ms of speech end.

---

## 3. Always-Streaming vs Mixed vs Batch — Comparative Analysis

### 3.1 Option A: Always-Streaming (Deepgram-driven dispatch)

**Architecture:** WAKE_DETECTED → open live WebSocket → ring tail sends audio → Deepgram `UtteranceEnd` or `speech_final` triggers dispatch → close WebSocket and call Claude.

**Latency profile:**
- First transcript available ~300–500ms after speech ends (vs ~1.8s batch).
- End-to-end latency (wake→response) reduced by ~1.3–1.5s.
- **Risk:** Deepgram `endpointing` defaults to 10ms — fires on any pause, which is too aggressive. Must configure `utterance_end_ms=1500–2000` and rely on `UtteranceEnd` events. This requires `interim_results=True` and careful state machine logic to avoid partial dispatches.

**Dictation-mode compatibility:**
- `utterance_end_ms` max documented useful value is not explicitly capped, but very high values (>10s) are untested territory. Deepgram's own docs recommend the feature for noise rejection, not for extended silence tolerance.
- Long silences between dictation sentences will fire `UtteranceEnd` events mid-thought, triggering premature dispatch.
- **Mitigation:** Suppress `UtteranceEnd` dispatch and only dispatch on explicit user signal (button), or accumulate across multiple `UtteranceEnd` events with a global timer. This significantly increases state machine complexity.

**VAD tuning complexity:** High. You are now tuning two independent VADs (Silero for write-to-ring, Deepgram for dispatch) with no shared state. Reconciling their different silence thresholds while maintaining correct ring buffer span boundaries is non-trivial.

**Implementation risk on Pi 4:** Medium-high. Requires thread-safe dispatch from WebSocket callback to cognitive loop; ring buffer span boundaries become ambiguous (write_pos at Deepgram event arrival vs VAD_STOPPED); ring tail thread must handle KeepAlive during silence; increased network dependency for dispatch timing.

**Ring buffer interaction:** The ring tail thread continuously drains from wake_pos; the dispatch happens on Deepgram event, not on ring state. You lose the clean `VAD_STOPPED → read ring span from wake_pos to write_pos` pattern. Must snapshot write_pos at dispatch time, but Deepgram `last_word_end` is in audio-stream time, not ring-buffer byte offset.

**Verdict:** Higher latency benefit, higher implementation complexity and risk. Appropriate for EU-7+ after the simpler pattern is validated.

### 3.2 Option B: Mixed Mode (Silero-dispatched, Deepgram-streamed) — RECOMMENDED

**Architecture:** WAKE_DETECTED → open live WebSocket → ring tail sends audio continuously → VAD_STOPPED → `send_finalize()` → `send_close_stream()` → read accumulated `is_final` transcripts → call Claude.

**Latency profile:**
- Transcription begins immediately on wake, so by the time VAD_STOPPED fires, most transcript is already available in the accumulator.
- Net STT latency: effectively near-zero (Deepgram has been processing the audio in real time); the only remaining latency is the final `send_finalize()` flush round-trip (~50–150ms).
- Total gain over batch: ~1.5–1.8s.

**Dictation-mode compatibility:**
- Silero `stop_secs` controls dispatch timing. Increase from 1.8s to 25–30s for dictation mode.
- Deepgram KeepAlive maintains the WebSocket during silence gaps.
- Word timestamps in transcripts remain accurate (KeepAlive does not affect them).
- The cognitive loop receives the complete utterance transcript accumulated over the full session.

**VAD tuning complexity:** Low. Single VAD (Silero) remains authoritative. Deepgram endpointing can be left at default or disabled — it is irrelevant for dispatch.

**Implementation risk on Pi 4:** Low. Matches the EU-5 spec exactly. Ring buffer pattern unchanged. Master only adds: (a) a ring-tail thread, (b) a KeepAlive thread, (c) a transcript accumulator, (d) connection open/close around the capture session.

**Ring buffer interaction:** Clean. Wake_pos and end_pos (VAD_STOPPED write_pos) are still the canonical span. The ring tail thread reads write_pos to know where to send up to; master reads the same position.

**Verdict:** This is the right option for EU-5. Implement this.

### 3.3 Option C: Batch-Only (current EU-4)

**Architecture:** VAD_STOPPED → read ring buffer → write WAV → HTTP REST → transcript.

**Latency profile:** ~1.7–2.0s STT latency after speech ends.

**Dictation-mode compatibility:** Full, with `stop_secs` tuning.

**Implementation risk:** None (already deployed).

**Verdict:** Acceptable for prototyping, but the user experience of a ~2s STT pause after every utterance is perceptible and should be addressed in EU-5.

### 3.4 Summary Table

| Dimension | Always-Streaming | Mixed (Silero-dispatch) | Batch (current) |
|-----------|-----------------|------------------------|-----------------|
| STT latency | ~300–500ms | ~50–150ms final flush | ~1.7–2.0s |
| Dictation compat. | Poor (UtteranceEnd fires mid-pause) | Good (stop_secs tunable) | Good |
| Complexity | High | Low-Medium | None |
| Pi 4 impl. risk | Medium-High | Low | None |
| Ring buffer fit | Awkward | Clean | Clean |
| Recommended | EU-7+ | **EU-5 ✓** | Current (EU-4) |

---

## 4. Dictation Mode Design Recommendations

### 4.1 Silero VAD Parameter Tuning

The current `stop_secs=1.8` is appropriate for conversational mode. The user's requirement of 20–30s natural pauses between sentences requires a much larger value.

**[Inference, confirmed by Silero VAD design]** `stop_secs` is the silence duration (in seconds) after which `VAD_STOPPED` is sent. Setting it to 25–30s means Silero will wait for 25–30s of continuous silence before signalling stop.

**Recommended `stop_secs` values by mode:**

| Mode | stop_secs | Notes |
|------|-----------|-------|
| Conversational | 1.8s (current) | Good for quick back-and-forth |
| Dictation | 25–30s | Tolerates sentence-length pauses |

**`start_secs=0.2` (current):** This controls how long speech must be present before VAD reports onset. It is appropriate for both modes — no change needed.

**Tradeoffs of large `stop_secs`:**
- If the user says "done" or falls silent permanently, the system will wait the full `stop_secs` before dispatching. At 30s, this means up to 30s of unresponsive silence before the cognitive loop fires. This is the primary motivation for a 'finished' button.
- Ring buffer capacity is 512KB (~16.4s at 16kHz int16). A 30s pause within a capture session means ~30s of silence writing into the ring. If total capture duration (speech + pauses) exceeds 16.4s, the ring buffer wraps. The master's `ring_reader.is_stale(wake_pos)` check will detect this, but the audio will be truncated. **For dictation mode with long utterances, consider whether the 512KB ring is sufficient.** At ~60 words/minute dictation, 16.4s captures ~16 words — likely insufficient for the intended use case. Consider increasing `RING_SIZE` for dictation mode (e.g., 2MB ≈ 65s capacity).
- Always-streaming (Option A above) eliminates the ring capacity concern by forwarding audio directly to Deepgram, but at the cost of complexity described in §3.1.

### 4.2 Dual-Mode Strategy (Recommended)

Rather than a single `stop_secs` value, implement a mode flag that can be set at runtime:

**[Inference]** The cleanest mechanism given the current architecture:

1. Add a `mode` field to the `SET_CAPTURE` command the master sends to the child on WAKE_DETECTED: `{"cmd": "SET_CAPTURE", "mode": "conversational" | "dictation"}`.
2. The child's `command_listener` reads `mode` and configures the GatedVADProcessor's `stop_secs` accordingly before entering CAPTURE phase.
3. Mode is selected by the master based on context (e.g., a toggle command, environment variable, or future UI).

This requires no structural changes to the recorder child beyond passing `stop_secs` through at SET_CAPTURE time, which is a small addition to the existing command routing in `recorder_state.py`.

### 4.3 'Finished' Button Integration

**[Research-confirmed architecture, inference on implementation]**

The existing pipe protocol supports a `FORCE_DISPATCH` command. The master can send this to the child at any time, and the child's `command_listener` can handle it by calling `state.set_phase("idle")` immediately (bypassing VAD timeout). This is analogous to the existing `SET_IDLE` command but initiated by user action rather than VAD.

**Implementation sketch:**

1. Add a new pipe command: `{"cmd": "FORCE_DISPATCH"}`.
2. In the child's `command_listener` routing table, map `FORCE_DISPATCH` → `state.signal_force_dispatch()`.
3. In `RecorderState.signal_force_dispatch()`, if current phase is `"capture"`, immediately call the same logic as `signal_vad_stopped()` (send VAD_STOPPED event upstream, transition to idle).
4. In the master, the 'finished' button (keyboard key, GPIO interrupt, etc.) calls `pipe.send({"cmd": "FORCE_DISPATCH"})`.
5. The master receives `VAD_STOPPED` as normal — no other master-side changes.

This approach is fully backwards-compatible and requires ~15–20 lines across `recorder_state.py` and the child command handler.

### 4.4 Concrete Recommendation

For EU-5 and the near term, implement the following:

1. **Default mode: conversational** (`stop_secs=1.8s`) — no change from current.
2. **Dictation mode: opt-in via env var** `DICTATION_MODE=1` → `stop_secs=28s`, `RING_SIZE=2MB` (if audio span may exceed current 16.4s).
3. **Add `FORCE_DISPATCH` command** as a ~20-line addition in `recorder_state.py` and the child's command handler.
4. **KeepAlive thread in ring tail** — required for both modes when using streaming (Option B).

---

## 5. Other Findings

### 5.1 Deepgram Nova-3 Streaming Mode Caveats

**[Research-confirmed]**

- **WER gap vs batch:** 6.84% vs 5.26% WER. Most pronounced for short utterances, numerics, and proper nouns. For dictation of natural prose, the degradation is less perceptible than the raw WER suggests.
- **No known critical bugs** as of April 2026. The Nova-3 streaming changelog (February 2026) lists only improvements: enhanced word timestamps, English formatting, reduced numeric WER.
- **`smart_format=True` with `endpointing`:** Deepgram may hold off on finalizing number sequences when `smart_format=True` to allow correct formatting (e.g., phone numbers). This can add a few hundred ms to finalization on numeric content.
- **Concurrent WebSocket sessions from the same API key:** Not documented as limited. Pi 4 (single session) is not a concern here.

### 5.2 OpenWakeWord 0.4.0 Limitations

**[Research-confirmed]**

- **Stuck at 0.4.0 on ARM64:** `pip install openwakeword` defaults to 0.4.0; v0.6.0 fails with missing `tflite-runtime` wheels on Python 3.11+ ARM64. This is a known open issue as of March 2026 (GitHub issue #322).
- **API difference:** 0.4.0 uses `wakeword_model_paths` parameter; 0.6.0 uses `wakeword_models`. The Pipecat 0.0.108 OpenWakeWord processor is written for 0.4.0 — do not upgrade without testing against the Pipecat layer.
- **hey_jarvis model:** The bundled model `hey_jarvis` works on 0.4.0. False-positive rate is manageable with the existing 5-buffer OWW full reset on each ungate transition.
- **Alternatives:** Picovoice Porcupine (paid, more accurate, good ARM support), whisper-based always-on (higher CPU), Wyoming Satellite OpenWakeWord integration (uses 0.6.0, not compatible with current stack). No drop-in replacement for 0.4.0 without significant refactoring.
- **Action:** None required for EU-5. Pin at 0.4.0 as documented in `AGENTS.md`.

### 5.3 Pipecat 0.0.108 EOL Status

**[Research-confirmed]**

- **Not EOL.** Pipecat 0.0.108 was released 2026-03-27/28 and is currently marked "Production/Stable" on PyPI. The project is under active development.
- **Version numbering:** Pipecat uses sequential 0.0.x versioning — this is not a "0.0.x as pre-release" signal; it's just the project's versioning scheme.
- **Successor patterns:** No announced breaking API change. The `process_frame` override requirement documented in `memory/pipecat_learnings.md` is still correct.
- **New in recent versions:** `DeepgramFluxSageMakerSTTService`, `AssemblyAI` improvements, `SarvamLLMService`, `NovitaLLMService`, `XAIHttpTTSService`. None of these affect the current recorder child design.
- **Action:** No action required. The forked assistant uses Pipecat only in the recorder child (core 0), which is not changing in EU-5.

### 5.4 Pi 4 ARM64 ONNX/Threading Issues

**[Research-confirmed]**

- **Silero ONNX on ARM:** Confirmed working. Silero VAD ONNX v5 runs at ~189μs per 31.25ms chunk (~165× real-time) on a single thread. The Pi 4 constraint of single-threaded ONNX is the right configuration.
- **Threading pins:** The existing `session.intra_op_num_threads=1 / inter_op_num_threads=1` approach is the standard recommendation for embedded ONNX. These are already implied by the non-overlapping OWW/Silero phase design.
- **EU-5 threading concern:** The ring tail thread (polling at 20ms) will run on the master process (cores 1–3). It does not interact with ONNX. The only new threading in EU-5 is pure Python + network I/O — no ONNX contention risk.
- **WebSocket thread:** The Deepgram SDK's `start_listening()` is blocking and should run in a daemon thread in the master process. This is standard and well-tested on ARM64 Linux.
- **Action:** No special ONNX precautions needed for EU-5.

### 5.5 Contemporary Streaming STT+LLM Pipeline Patterns

**[Research-confirmed, inference on applicability]**

Modern production voice agents (2025–2026) target <500ms end-to-end latency through:

1. **Streaming at every stage:** STT (live WebSocket), LLM (token streaming), TTS (chunk-based synthesis). This matches EU-5 (streaming STT) and EU-6 (streaming LLM output).
2. **TTFT optimization:** LLM time-to-first-token is the dominant latency component (typically 0.4–4.7s). For Claude Haiku on the Pi, this is the primary latency target after EU-5 addresses STT.
3. **VAD optimization:** Reducing silence detection from 500–800ms to the minimum. The current 1.8s `stop_secs` is conservatively large; 800ms–1.0s is sufficient for most conversational use and would reduce perceived latency further.
4. **Edge deployment:** Pi 4 is appropriate for this architecture. CPU-intensive operations (ONNX, audio processing) are isolated in the recorder child; the cognitive loop (master) handles only network I/O and subprocess management.

---

## 6. EU-5 Implementation Notes

### 6.1 Corrected API Call Pattern

Replace the EU-5 spec's `dg_client.listen.live.v("1")` with the current SDK pattern:

```python
from deepgram import DeepgramClient
from deepgram.core.events import EventType

dg_client = DeepgramClient()  # reads DEEPGRAM_API_KEY from env

# In the cognitive loop, opened on WAKE_DETECTED:
with dg_client.listen.v1.connect(
    model="nova-3",
    encoding="linear16",
    sample_rate=16000,
    channels=1,
    language="en-US",
    smart_format=True,
    interim_results=True,
    endpointing=300,           # 300ms silence triggers speech_final; advisory only
) as connection:
    transcripts: list[str] = []

    def on_message(message) -> None:
        if hasattr(message, 'channel'):
            text = message.channel.alternatives[0].transcript.strip()
            if message.is_final and text:
                transcripts.append(text)

    connection.on(EventType.MESSAGE, on_message)
    connection.on(EventType.ERROR, lambda e: logger.error("DG WS error: %s", e))

    # Start listening loop in a daemon thread:
    listen_thread = threading.Thread(target=connection.start_listening, daemon=True)
    listen_thread.start()

    # Ring tail thread runs here, feeding audio and KeepAlives...
    # (see §6.2)

    # On VAD_STOPPED:
    connection.send_finalize()
    time.sleep(0.2)  # allow final transcript to arrive
    connection.send_close_stream()
    listen_thread.join(timeout=3.0)

full_transcript = " ".join(transcripts)
```

### 6.2 Ring Tail Thread with KeepAlive

```python
def ring_tail_thread(ring_reader, connection, stop_event):
    """Poll ring buffer at 20ms intervals, send new bytes to Deepgram WebSocket.
    
    Sends KeepAlive when no new audio to prevent 10s timeout.
    stop_event is set by master when VAD_STOPPED is received.
    """
    POLL_INTERVAL = 0.020       # 20ms
    KEEPALIVE_INTERVAL = 4.0    # send KeepAlive every 4s during silence
    
    read_pos = ring_reader.write_pos  # start from current position (post-wake)
    last_audio_time = time.monotonic()
    last_keepalive_time = time.monotonic()

    while not stop_event.is_set():
        current_wp = ring_reader.write_pos
        if current_wp > read_pos:
            # New audio available — send it
            audio_bytes = ring_reader.read(read_pos, current_wp)
            if audio_bytes:
                connection.send_media(audio_bytes)
                last_audio_time = time.monotonic()
                last_keepalive_time = time.monotonic()  # reset KA timer
            read_pos = current_wp
        else:
            # Silence — check if KeepAlive needed
            now = time.monotonic()
            if now - last_keepalive_time >= KEEPALIVE_INTERVAL:
                connection.send_keep_alive()
                last_keepalive_time = now
        
        time.sleep(POLL_INTERVAL)
```

### 6.3 Integration with Master Event Loop

The ring tail thread and WebSocket connection span from `WAKE_DETECTED` to `VAD_STOPPED`. The master currently runs the cognitive loop synchronously in `master_loop`. For EU-5, the pattern is:

1. On `WAKE_DETECTED`: open WebSocket connection, snapshot `wake_pos`, create `stop_event`, start ring tail thread and listen thread.
2. On `VAD_STOPPED`: set `stop_event`, send `send_finalize()` + `send_close_stream()`, join threads, collect transcript.
3. Pass collected transcript to `cognitive_loop()` (same interface as today).

The existing `cognitive_loop` signature `(audio_bytes, dg_client)` will change to `(transcript, dg_client)` or the transcript accumulator can be integrated directly. The `transcribe()` function becomes a no-op for streaming mode.

### 6.4 `send_finalize()` Purpose

`send_finalize()` instructs Deepgram to immediately return any pending partial transcripts as final results. Without it, Deepgram may hold a partial result waiting for more audio context. Sending it before `send_close_stream()` ensures you capture the last few words of the utterance.

### 6.5 Transcript Accumulation

`is_final=True` results are stable — Deepgram guarantees they will not be revised. Interim results (`is_final=False`) are unstable and should not be accumulated for final dispatch. The accumulator pattern:

```python
# Accumulate is_final segments in order:
transcripts: list[str] = []
# Append only when is_final=True and text is non-empty
# Join with " " at dispatch time
full_transcript = " ".join(transcripts)
```

Note: `speech_final=True` can appear on a `is_final=False` result (Deepgram's endpointing fired but the result isn't finalized yet). For EU-5 mixed mode, wait for `is_final=True`.

### 6.6 EU-6 Streaming Claude — `--output-format stream-json`

**[Research-confirmed]** For EU-6, replace `subprocess.run(["claude", "-p", ...])` with:

```python
import subprocess, json

def run_claude_streaming(transcript: str) -> None:
    proc = subprocess.Popen(
        ["claude", "-p", transcript,
         "--model", "claude-haiku-4-5-20251001",
         "--output-format", "stream-json",
         "--verbose"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        # Each line may contain one or more JSON objects (buffering can concatenate)
        # Split on "}\n{" boundaries handled by line iteration if bufsize=1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Filter for text delta events
        if (obj.get("type") == "stream_event"
                and obj.get("event", {}).get("type") == "content_block_delta"
                and obj.get("event", {}).get("delta", {}).get("type") == "text_delta"):
            text = obj["event"]["delta"]["text"]
            print(text, end="", flush=True)
    proc.wait()
    print()  # final newline
```

**Known issue:** A regression in Claude Code v2.1.83 caused empty stdout with `-p` when piped (GitHub issue #38611). As of March 2026 this is documented as an open issue. If `subprocess.run` with `capture_output=True` returns empty output despite returncode=0, switch to `--output-format stream-json` with `Popen` as above — this path does not trigger the regression.

---

## References

1. [Deepgram — Getting Started with Live Streaming Audio](https://developers.deepgram.com/docs/live-streaming-audio)
2. [Deepgram — Endpointing](https://developers.deepgram.com/docs/endpointing)
3. [Deepgram — Understanding End of Speech Detection](https://developers.deepgram.com/docs/understanding-end-of-speech-detection)
4. [Deepgram — Utterance End](https://developers.deepgram.com/docs/utterance-end)
5. [Deepgram — Audio Keep Alive](https://developers.deepgram.com/docs/audio-keep-alive)
6. [DeepWiki — Real-time Streaming (Listen v1 & v2)](https://deepwiki.com/deepgram/deepgram-python-sdk/3.2-real-time-streaming-(listen-v2))
7. [Vapi AI Blog — Deepgram Nova-3 vs Nova-2](https://vapi.ai/blog/deepgram-nova-3-vs-nova-2)
8. [Deepgram STT Changelog 2025-02-12](https://developers.deepgram.com/changelog/speech-to-text-changelog/2025/2/12)
9. [GitHub — anthropics/claude-code Issue #1901: Streaming output](https://github.com/anthropics/claude-code/issues/1901)
10. [GitHub — anthropics/claude-code Issue #38611: CLI -p regression](https://github.com/anthropics/claude-code/issues/38611)
11. [GitHub — dscripka/openWakeWord Issue #322: Raspberry Pi install](https://github.com/dscripka/openWakeWord/issues/322)
12. [PyPI — openwakeword v0.6.0](https://pypi.org/project/openwakeword/)
13. [PyPI — pipecat-ai v0.0.108](https://pypi.org/project/pipecat-ai/0.0.108/)
14. [Silero VAD — Performance Metrics Wiki](https://github.com/snakers4/silero-vad/wiki/Performance-Metrics)
15. [DEV Community — Voice AI Agent Latency Optimization](https://dev.to/sidharth_grover_0c1bbe66d/from-5-seconds-to-07-seconds-how-i-built-a-production-ready-voice-ai-agent-and-cut-latency-by-7x-52ig)
16. [anthropics/claude-agent-sdk-python — SubprocessCLITransport](https://github.com/anthropics/claude-code-sdk-python/blob/7297bdcb/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)
