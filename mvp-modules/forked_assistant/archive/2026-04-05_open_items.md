# Open Items — 2026-04-05

Session covered EU-7 proof (first speech from agent), live-sentence streaming
(EU-7b), re-emission fix, and markdown stripping. Three open items remain before
step 9 (looping) can be formally validated.

---

## 1. TTS rearchitecture — blocking

**Status:** design decision required before next Pi session.

**What happened:** OOM killer fired during Piper synthesis on the 1 GB Pi 4
(`en_US-lessac-medium`, ~63 MB ONNX). At time of kill, master.py had 317 MB RSS
+ 385 MB in swap = ~700 MB footprint. Total swap (900 MB) was exhausted. The
kernel sent SIGKILL to master.py; the recorder_child survived and ran until
Pipecat's built-in idle timeout fired (~4 min later).

Additionally, audio tearing was observed on the existing Piper model during
playback — quality is below acceptable threshold for intended use cases.

**Disposition:** off-device TTS synthesis is under consideration. Candidate
options not yet evaluated:
- ElevenLabs streaming API
- Cartesia (low-latency streaming)
- Deepgram Aura
- Kokoro (cloud-hosted or lightweight ONNX variant)

**What's needed:** select a TTS target, evaluate latency and quality against
the 1 s first-audio target, then replace `PiperTTS` in `src/tts.py` with the
new backend. `master.py` integration point is `tts.play(agent.run(transcript))`
— any implementation that accepts an `Iterator[str]` and plays audio satisfies
the interface.

`src/tts.py` and the `PiperTTS` class remain as a reference implementation and
can be archived once a replacement is proven.

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

## 3. Step 9 formal validation — blocked on item 1

**Status:** deferred until TTS is stable.

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
