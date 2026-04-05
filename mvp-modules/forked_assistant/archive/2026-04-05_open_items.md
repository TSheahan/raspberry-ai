# Open Items — 2026-04-05

Session covered EU-7 proof (first speech from agent), live-sentence streaming
(EU-7b), re-emission fix, and markdown stripping. Three open items remain before
step 9 (looping) can be formally validated.

---

## 1. TTS rearchitecture — RESOLVED (sessions 1–5, 2026-04-05)

**Status:** evaluation and tuning complete. `master.py` swap pending (Phase 3 Pi run).

**What happened:** OOM killer fired during Piper synthesis on the 1 GB Pi 4
(`en_US-lessac-medium`, ~63 MB ONNX). At time of kill, master.py had 317 MB RSS
+ 385 MB in swap = ~700 MB footprint. Audio tearing also observed.

**Resolution:** All three cloud backends evaluated and tuned. Platform precedence
and voice selections locked. `tts.py` defaults set — `CartesiaTTS()` requires no args.

| Priority | Backend | Voice | Role |
|----------|---------|-------|------|
| 1 | Cartesia | Allie (`2747b6cf-...`) | Primary — ~49 MB RSS, streaming |
| 2 | ElevenLabs | Matilda (`XrExE9yK...`) | Fallback — ~350ms warm first-chunk |
| 3 | Deepgram | Helena (`aura-2-helena-en`) | Tertiary — REST-only, click artifact |

**Remaining:** swap `PiperTTS` → `CartesiaTTS` in `master.py`, wire `tts.warm()` at
`WAKE_DETECTED` (threaded). See `forked_assistant/AGENTS.md § Phase 3` for exact
changes. Full record: `archive/tts_evaluation/`.

---

## 2. Recorder child orphan detection — low priority

**Status:** known gap; currently acceptable via Pipecat idle timeout.

**What happened:** when master.py was OOM-killed, the recorder_child lost its
parent pipe but had no code-level detection. It ran for ~4 min printing DUTY
cycles until Pipecat's idle timeout cancelled the pipeline. It then exited
cleanly via the normal SHUTDOWN_FINISHED path.

**Disposition:** Pipecat idle timeout is a functional implicit safeguard (though
slow). A proper fix would be periodic PPID monitoring in the recorder_child:
record `_expected_ppid = os.getppid()` at startup; if `os.getppid() == 1` in
the duty-cycle or frame-processing path, initiate shutdown. This prevents
orphan accumulation across repeated master crashes.

Priority is low — the Pipecat timeout works, and if TTS moves off-device the
OOM scenario becomes less likely. Revisit after TTS decision.

---

## 3. Step 9 formal validation — unblocked (pending Phase 3 Pi run)

**Status:** unblocked once Phase 3 (CartesiaTTS swap + warm() + Pi run) completes.

**What's needed:** 3–5 complete turns with latency measurements table filled
(per `starting_brief.md`). The pipeline already loops correctly (SET_WAKE_LISTEN
after cognitive_loop returns); this is a measurement exercise, not a code change.

**Latency observations so far (2 runs):**
- STT: 451–519 ms ✓ (target < 1 s)
- Agent first token: 6–14 s (Cursor agent cold-start + optional tool call;
  live-sentence streaming means first audio comes at first sentence boundary,
  not end-of-stream)
- TTS (Piper): OOM on medium model; audio tearing observed — not suitable

---

## Context: what is complete

| EU | Description | Status |
|----|-------------|--------|
| EU-1 through EU-6 | SharedMemory, ring buffer, recorder child, STT, agent | Complete |
| EU-7 | PiperTTS integration + live-sentence streaming | Code proven; Piper unsuitable for 1 GB Pi — see item 1 |

Steps 1–7 of `starting_brief.md` are functionally complete. Step 8 (TTS audio
output) is proven as a pipeline integration but requires a TTS backend swap
before it can be considered delivered. Step 9 follows step 8.
