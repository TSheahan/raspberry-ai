# Pipecat PoC Brief — End-to-End Pipeline Integration

**Date:** 2026-03-30
**Purpose:** Build and exercise a complete end-to-end voice pipeline on `morpheus` using resolved components. Confirms Pipecat as the platform or triggers LiveKit migration. This is the final design-stage demo — everything before this point has been precursor work.
**Depends on:** STT ✓ (Deepgram Nova-3), Wake word ✓ (openWakeWord 0.4.0), Pi OS ✓ (Trixie), Pipecat venv ✓ (`~/pipecat-agent/venv/`)

---

## Host environment

| Item | Value |
|---|---|
| Device | Raspberry Pi 4 Model B |
| Hostname | `morpheus` |
| Static IP | `192.168.1.21` |
| OS | Raspberry Pi OS Trixie 64-bit (Debian 13, Lite) |
| Kernel | `6.12.75+rpt-rpi-v8` (aarch64) |
| Python venv | `~/pipecat-agent/venv/` |
| Already installed | `pipecat-ai[local,silero]` 0.0.108, `pyaudio` 0.2.14, `torch` 2.11.0+cpu, `openwakeword` 0.4.0, `numpy` 2.4.3 |
| Audio input | ReSpeaker 4-Mic Array, PyAudio device index `1` |
| Audio output | bcm2835 headphones (3.5mm jack), PyAudio device index `0` (S16_LE only) |
| Audio subsystem | ALSA only — no PulseAudio, no PipeWire |
| Desktop (agentic) | `claude -p` via SSH subprocess call; desktop runs Claude Code with KB project context |

---

## Architecture — what to build

Single process, single venv. Sequence on each turn:

```
[ReSpeaker mic]
    -> openWakeWord (in-process, OpenWakeWordProcessor) — listens for "hey_jarvis"
    -> VAD (Silero, built-in to pipecat-ai[silero]) — detects end of utterance
    -> Deepgram Nova-3 STT (file-based, deepgram-sdk) — transcribes captured audio
    -> claude -p subprocess — sends transcript, captures text response
    -> Piper TTS — synthesises response audio
    -> [bcm2835 headphone output]
```

All audio I/O via `LocalAudioTransport` with the ReSpeaker (input) and bcm2835 (output) device indices.

---

## What to install

Add to the existing venv:

```bash
source ~/pipecat-agent/venv/bin/activate
pip install deepgram-sdk piper-tts
```

- `deepgram-sdk` — same library used in the STT benchmark
- `piper-tts` — local TTS; needs a voice model downloaded (see below)
- `openwakeword` 0.4.0 should already be present from the wake word demo; verify with `pip show openwakeword`

If `openwakeword` is not in the main venv (it was installed in a separate demo venv), install it:

```bash
pip install openwakeword==0.4.0
```

### Piper voice model

Download a voice model to the Pi (English, a neutral voice is fine — `en_US-lessac-medium` is a good default):

```bash
mkdir -p ~/piper-models
# Download from piper releases — check https://github.com/rhasspy/piper/releases for current model URLs
# Model file: en_US-lessac-medium.onnx + en_US-lessac-medium.onnx.json
```

The `.onnx` and `.onnx.json` files must be present together. Note the path — it's a constructor argument.

### Deepgram API key

The STT benchmark already used a Deepgram API key. Use the same key. It needs to be available as an environment variable:

```bash
export DEEPGRAM_API_KEY=your_key_here
```

Or place it in a `.env` file and load it in the script.

---

## Pipeline script

Build `~/pipecat-agent/voice_pipeline.py`. The structure below is the target — adapt imports and class names to match the actual pipecat-ai 0.0.108 API (check installed pipecat source if unsure).

```python
import asyncio
import subprocess
import os
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.frameworks.openai_llm_context import OpenAILLMContext  # may not apply
from pipecat.processors.wake_word import OpenWakeWordProcessor
# STT and TTS: pipecat may have built-in processors or you implement custom ones below

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
PIPER_MODEL_PATH = os.path.expanduser("~/piper-models/en_US-lessac-medium.onnx")


async def run_claude(transcript: str) -> str:
    """Subprocess call to claude -p on the desktop via SSH, or local if KB is on Pi."""
    # If KB project is on desktop, SSH + claude -p. For initial PoC, simpler:
    # run claude -p locally (requires claude CLI installed on Pi and authed, or proxy to desktop).
    # See notes below on agentic layer integration approach.
    result = subprocess.run(
        ["claude", "-p", transcript, "--model", "claude-haiku-4-5-20251001"],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


async def main():
    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_in_device_index=1,  # ReSpeaker
        audio_out_enabled=True,
        audio_out_device_index=0,  # bcm2835
        vad_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
        vad_audio_passthrough=True,
    ))

    # Wake word processor — "hey_jarvis" is a built-in keyword (no expiry)
    wake_word = OpenWakeWordProcessor(wake_words=["hey_jarvis"])

    # STT: custom processor wrapping deepgram-sdk file-based transcription
    # (implement as a Pipecat FrameProcessor — see notes below)

    # TTS: Piper — implement as a FrameProcessor or use pipecat's built-in if available

    # Pipeline assembly
    pipeline = Pipeline([
        transport.input(),
        wake_word,
        # stt_processor,
        # llm_processor,  # wraps run_claude()
        # tts_processor,
        transport.output(),
    ])

    runner = PipelineRunner()
    task = PipelineTask(pipeline)
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
```

**Custom processor pattern** — pipecat-ai 0.0.108 uses a `FrameProcessor` base class. STT and TTS integrations that aren't built-in need to be implemented as subclasses. The minimal interface:

```python
from pipecat.frames.frames import AudioRawFrame, TextFrame, Frame
from pipecat.processors.frame_processor import FrameProcessor

class DeepgramSTTProcessor(FrameProcessor):
    """Receives AudioRawFrame, emits TextFrame with transcript."""
    async def process_frame(self, frame: Frame, direction):
        if isinstance(frame, AudioRawFrame):
            # write audio to temp file, call deepgram-sdk, emit TextFrame
            ...
        await self.push_frame(frame, direction)

class PiperTTSProcessor(FrameProcessor):
    """Receives TextFrame, emits AudioRawFrame with synthesised audio."""
    async def process_frame(self, frame: Frame, direction):
        if isinstance(frame, TextFrame):
            # call piper-tts, emit AudioRawFrame(s)
            ...
        await self.push_frame(frame, direction)
```

Refer to pipecat's existing STT/TTS processor implementations in the installed source for the correct frame types and patterns. Check:

```bash
find ~/pipecat-agent/venv/lib -name "*.py" -path "*/stt/*" | head -5
find ~/pipecat-agent/venv/lib -name "*.py" -path "*/tts/*" | head -5
```

---

## Agentic layer integration — two approaches

**Option A: claude CLI on Pi (simplest for PoC)**
Install Claude Code CLI on `morpheus` and authenticate. The voice pipeline calls `claude -p transcript` as a local subprocess. The KB project isn't on the Pi, so this only gives bare Claude responses — no KB context, no project rules. Acceptable for PoC latency testing; not the final architecture.

```bash
# On morpheus:
npm install -g @anthropic-ai/claude-code  # or check current install method
claude auth  # authenticate with Pro account
```

**Option B: SSH subprocess to desktop (preferred for end-to-end test)**
The voice pipeline on the Pi SSHes to the desktop and runs `claude -p` there, where the KB project lives. Requires passwordless SSH from Pi to desktop.

```bash
# On morpheus:
ssh-keygen -t ed25519  # if no key exists
ssh-copy-id tim@<desktop-ip>

# In pipeline code:
result = subprocess.run(
    ["ssh", "tim@<desktop-ip>", "cd /path/to/kb && claude -p", transcript],
    capture_output=True, text=True, timeout=30
)
```

**For the PoC:** start with Option A to validate pipeline mechanics and latency without SSH complexity. Switch to Option B once the pipeline is running end-to-end.

---

## Build sequence

Build and test incrementally — don't assemble the full pipeline before verifying each layer.

### Step 1 — Verify wake word in venv
```bash
source ~/pipecat-agent/venv/bin/activate
python -c "import openwakeword; print(openwakeword.__version__)"
# Expect: 0.4.0
```
If not present, `pip install openwakeword==0.4.0`.

### Step 2 — Verify Deepgram STT still works
Re-run the benchmark script from `~/stt-benchmark/` with a test file. Confirms the API key and deepgram-sdk are still functional.

### Step 3 — Verify Piper TTS standalone
```bash
echo "Hello, this is a test of the Piper voice." | \
  python -m piper --model ~/piper-models/en_US-lessac-medium.onnx --output-raw | \
  aplay -r 22050 -f S16_LE -c 1 -
```
Confirm audio plays through the 3.5mm jack. Adjust sample rate to match the model's native rate if no sound.

### Step 4 — Minimal pipeline: wake word → print
Build the pipeline with just `LocalAudioTransport` + `OpenWakeWordProcessor`. On wake word detection, print "WAKE DETECTED" and exit. Confirm the wake word fires reliably in the room environment.

### Step 5 — Add VAD → capture utterance
Add Silero VAD after wake word. Capture the utterance that follows the wake word. Print audio length and stop. Confirm VAD correctly segments the utterance.

### Step 6 — Add STT → print transcript
Feed captured audio to `DeepgramSTTProcessor`. Print the transcript. Confirm transcription is accurate on normal speech.

### Step 7 — Add agentic layer → print response
Feed transcript to `run_claude()`. Print the response text. Note total latency from end-of-utterance to response text. This is the first full cognitive loop.

### Step 8 — Add TTS → audio output
Feed response text to `PiperTTSProcessor`. Play synthesised audio. Confirm the full turn completes: speech in → speech out.

### Step 9 — Loop: return to wake word listening
After TTS output completes, return to wake word listening. Exercise 3–5 complete turns.

---

## Measurements to record

For each complete turn (end-of-utterance to start of TTS audio output):

| Measurement | Target | Notes |
|---|---|---|
| STT latency | < 1s | Time from utterance end to transcript available |
| Claude -p latency | < 4s | Time from transcript to response text (first token acceptable) |
| TTS latency (first audio) | < 1s | Time from response text to first audio sample |
| Total turn latency | < 6s | End-of-utterance to first audio output |
| Wake word false positives | 0 per 30 min | TV on in background |
| Wake word missed detections | < 1 in 10 | Normal speaking volume, 1m distance |
| CPU during STT | < 15% | Deepgram is cloud — CPU should be near zero |
| CPU during TTS | < 50% | Piper on CPU — record peak |
| CPU during idle (wake word) | < 50% one core | openWakeWord observed ~40% one core |

---

## What to report back

### Per-step completion status

Brief note per step: complete / failed / skipped (and why).

### Latency observations

Fill the measurements table above from 3–5 representative turns. Note any outliers.

### Wake word behaviour

Any false positives or missed detections observed. Note environment (TV on/off, room noise).

### Issues encountered

Dependency conflicts, audio device problems, pipecat API surprises, anything requiring a workaround.

### TTS quality assessment

Is Piper output quality acceptable for the defined use cases (CPAP check-in, calendar query, bedtime routine)? Note any quality concerns vs the VoiceMode Whisper.cpp + cloud TTS baseline.

### Verdict

One of:
- **Pipecat confirmed** — pipeline runs end-to-end, latency within targets, no blocking issues. Ready for design document.
- **Pipecat with caveats** — runs but specific issues noted (e.g. barge-in unreliable, TTS quality marginal). Describe caveat and proposed resolution.
- **LiveKit migration triggered** — blocking issue that Pipecat cannot address. Describe blocker.

---

## Important constraints

- **Single venv** (`~/pipecat-agent/venv/`) unless a hard dependency conflict forces otherwise. If a conflict occurs, note it — this is the Plan B trigger condition documented in `design/2026-03-29_split-process-architecture.md`.
- **ALSA only** — no PulseAudio, no PipeWire, no desktop environment.
- **Audio input: device index `1`** (ReSpeaker). **Audio output: device index `0`** (bcm2835 headphones, S16_LE).
- **Do not modify project files on the desktop** from the Pi — this PoC runs entirely on `morpheus`.
- **Save all scripts to `~/pipecat-agent/`** on the Pi. These are retained build artifacts, not disposable.
- **openWakeWord version: 0.4.0** — do not upgrade; 0.6 breaks the shared venv.

---

## What this resolves

This demo closes the **platform fork** (Pipecat vs LiveKit) and confirms end-to-end pipeline viability. On completion, the design stage is functionally complete. The TTS fork (Piper vs Piper+Kokoro hybrid) is the one remaining open question — the Piper quality assessment here will inform it, but it doesn't block the design document.

**After this demo:** design document can be written. Build stage follows.
