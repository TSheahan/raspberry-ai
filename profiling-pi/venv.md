# Python venv — Dependency Profiles

Package installs into `/home/voice/venv/` on morpheus (Pi 4). Each section
profiles one **direct** dependency — a package our source code imports. Transitive
dependencies (pulled in automatically by pip) are not profiled here; they are
verified as a group with `pip check`.

**Discipline:** a package enters the venv only after it is profiled in this file.
If it's not here, it doesn't get installed. After all profiled deps are installed,
run the integrity check at the bottom of this file.

**Prerequisites:**
- `voice-user-setup.md` — creates the `voice` user and the venv at `/home/voice/venv/`
- `pip-hardening.md` — hardens pip/uv before any packages are installed

**Install order matters.** Dependencies are ordered in this file so that each
section can be installed and verified immediately. Tier 4 (ML/pipeline) deps
must be installed in the order shown — torch before pipecat-ai. Follow the
sections top to bottom.

**Observed totals** (2026-04-05 provisioning run): 10 direct deps + 89 transitive
deps + pip + pip-audit = 101 packages. Venv size: 1.6 GB on disk. 375 MB RAM
available after full install on 1 GB Pi.

---

## Target State

- `/home/voice/venv/` contains the direct dependencies profiled in this file
- `pip check` reports no broken requirements
- `pip-audit` reports no known vulnerabilities (expected skips: flatbuffers, torch, torchaudio — non-standard version labels)
- pip 26.0.1 or later

---

## loguru

Structured logging. Used at module scope by every source file in the voice agent.
This is the one third-party dependency that causes an import-time failure if
missing — all SDK imports are deferred to construction time, but loguru is not.

**Imported by:** `master.py`, `tts.py`, `recorder_child.py`, `ring_buffer.py`,
`recorder_state.py`, `log_config.py`, `agent_session.py`

### Install

```bash
pip install loguru
```

### Verify

```bash
python -c "from loguru import logger; logger.info('loguru ok')"
# Expected: timestamp | INFO | loguru ok
```

---

## python-dotenv

Loads `/home/voice/.env` key-value pairs into `os.environ` at startup.

**Imported by:** `master.py`, `tts.py` (smoke test `__main__`)

### Install

```bash
pip install python-dotenv
```

### Verify

```bash
python -c "from dotenv import load_dotenv; print('python-dotenv ok')"
# Expected: python-dotenv ok
```

### Notes

- **Warning:** there is a different PyPI package called `dotenv` (0.9.9) which is
  *not* the same thing. If `pip list | grep dotenv` shows both `dotenv` and
  `python-dotenv`, remove the stray: `pip uninstall dotenv`.

---

## pyalsaaudio

Direct ALSA bindings. Replaces PyAudio (PortAudio) for audio output in the TTS
pipeline. PortAudio's callback thread causes tearing on bcm2835 — confirmed in
`tts_evaluation` session 2.

**Imported by:** `tts.py` (deferred, runtime, Linux only)

### Install

```bash
# Build dependency — compiles a C extension against libasound
sudo apt install -y libasound2-dev

pip install pyalsaaudio
```

### Verify

```bash
# 1. Import
python -c "import alsaaudio; print('pyalsaaudio ok')"

# 2. Can open hw:0,0 for playback
python -c "
import alsaaudio
pcm = alsaaudio.PCM(
    type=alsaaudio.PCM_PLAYBACK,
    device='hw:0,0',
    channels=1,
    rate=24000,
    format=alsaaudio.PCM_FORMAT_S16_LE,
    periodsize=4096,
)
pcm.close()
print('ALSA hw:0,0 ok')
"
```

### Notes

- Builds from source on the Pi (no pre-built aarch64 wheel on PyPI or piwheels).
  Requires `libasound2-dev` headers. Build takes ~30s.
- Linux-only. On Windows the codebase falls back to PyAudio automatically.
- `hw:0,0` bypasses dmix — correct for the Pi where master.py is the sole audio
  consumer.

---

## pyaudio

PortAudio Python bindings. **Input capture** for the recorder child: Pipecat’s
local transport opens the microphone via PyAudio. This is separate from TTS
output — on Linux, playback uses **pyalsaaudio** (above) because PortAudio’s
output path tears on bcm2835; capture still goes through PyAudio/PortAudio.

`pipecat-ai` does **not** list `pyaudio` as a pip dependency — it must be
installed explicitly or imports fail at runtime.

**Imported by:** Pipecat local audio stack (via `recorder_child.py` transport)

### Install

```bash
# Build dependency — PyAudio compiles against PortAudio headers
sudo apt install -y portaudio19-dev

pip install pyaudio
```

### Verify

```bash
python -c "import pyaudio; print('pyaudio', pyaudio.__version__)"
```

### Notes

- Builds from source on the Pi if no matching wheel (Python 3.13 / aarch64 may
  build locally; allow ~30s).
- Do not confuse with **pyalsaaudio**: both may be present — pyalsaaudio for ALSA
  playback in `tts.py`, PyAudio for mic capture in the recorder pipeline.

---

## deepgram-sdk

Deepgram STT (Nova-3 live WebSocket) and TTS (Aura-2 REST). Serves double duty.

**Imported by:** `master.py` (STT), `tts.py` (deferred, `DeepgramTTS`)

### Install

```bash
pip install deepgram-sdk
```

### Verify

```bash
python -c "from deepgram import DeepgramClient; print('deepgram-sdk ok')"

# End-to-end (requires DEEPGRAM_API_KEY in .env):
cd /home/voice/raspberry-ai
python mvp-modules/forked_assistant/src/tts.py -b deepgram
```

---

## cartesia

Cartesia TTS (WebSocket streaming). **Primary** TTS backend.

**Imported by:** `tts.py` (deferred, `CartesiaTTS`)

### Install

```bash
pip install cartesia
```

### Verify

```bash
python -c "from cartesia import Cartesia; print('cartesia ok')"

# End-to-end (requires CARTESIA_API_KEY in .env):
cd /home/voice/raspberry-ai
python mvp-modules/forked_assistant/src/tts.py -b cartesia
```

---

## elevenlabs

ElevenLabs TTS (streaming). **Fallback** TTS backend.

**Imported by:** `tts.py` (deferred, `ElevenLabsTTS`)

### Install

```bash
pip install elevenlabs
```

### Verify

```bash
python -c "from elevenlabs.client import ElevenLabs; print('elevenlabs ok')"

# End-to-end (requires ELEVENLABS_API_KEY in .env):
cd /home/voice/raspberry-ai
python mvp-modules/forked_assistant/src/tts.py -b elevenlabs
```

---

## numpy

Array operations in the recorder child (audio frame manipulation, VAD input).

**Imported by:** `recorder_child.py`, `recorder_state.py`

### Install

```bash
pip install numpy
```

### Verify

```bash
python -c "import numpy; print('numpy', numpy.__version__)"
```

### Notes

- Also a transitive dep of `openwakeword` and `pipecat-ai`, but listed here
  because our code imports it directly. Version conflicts between consumers
  are a known pain point on ARM — run `pip check` after all installs.

---

## openwakeword

Wake word detection. Runs ONNX inference for keyword spotting.

**Imported by:** `recorder_child.py`

### Install

```bash
pip install openwakeword
```

### Verify

```bash
python -c "import openwakeword; print('openwakeword ok')"
```

### Notes

- Pulls in `onnxruntime` as a transitive dep (not profiled separately — we don't
  import it directly).

---

## torch + torchaudio (pre-install for pipecat-ai)

PyTorch CPU and its audio companion. Required transitively by pipecat-ai (via
Silero VAD). Not imported directly by our code, but listed here because they
**must be installed before pipecat-ai** from the PyTorch CPU wheel index — PyPI
does not host CPU-only ARM64 wheels.

### Install

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```

This overrides the global index for this one command. The `+cpu` local version
label in the installed wheel is expected (e.g. `2.11.0+cpu`).

### Verify

```bash
python -c "import torch; print('torch', torch.__version__)"
# Expected: torch 2.11.0+cpu (or similar)
```

### Notes

- ~150 MB download, ~200 MB installed. Heaviest dep in the venv.
- `pip-audit` will skip these (local version label not on PyPI) — expected.

---

## pipecat-ai

Audio pipeline framework. Provides the transport, VAD, and frame-processing
abstractions used by the recorder child.

**Imported by:** `recorder_child.py`

**Requires:** torch + torchaudio installed first (see above).

### Install

```bash
pip install pipecat-ai
```

This is a heavy install (~2 minutes on Pi). It pulls in many transitive deps
including `transformers`, `onnxruntime`, `scipy`, `scikit-learn`, `numba`.
The `onnxruntime` pin (`~=1.23.2`) may downgrade a version installed by
openwakeword — pip handles this automatically.

### Verify

```bash
python -c "import pipecat; print('pipecat-ai ok')"
```

### Notes

- Version 0.0.108 is the current known-good pin. This is a fast-moving package —
  verify compatibility with the recorder child before upgrading.

---

## piper-tts (archived)

Local ONNX TTS engine. **Archived** — too resource-intensive for 1 GB Pi 4, and
noticeably lower voice quality than the cloud backends. Retained for testing on
beefier hardware.

**Imported by:** `tts.py` (deferred, `PiperTTS`)

### Install

```bash
pip install piper-tts
```

### Verify

```bash
python -c "from piper.voice import PiperVoice; print('piper-tts ok')"

# End-to-end (requires model file — see piper-tts-setup.md):
cd /home/voice/raspberry-ai
python mvp-modules/forked_assistant/src/tts.py -b piper
```

### Notes

- Only install if Piper testing is needed. Not required for production.
- See `piper-tts-setup.md` for model download and PIPER_MODEL_PATH setup.

---

## Integrity Check

Run after all profiled deps are installed. This catches version conflicts,
known CVEs, and unexpected packages.

```bash
source /home/voice/venv/bin/activate

# 0. Install the audit tool (one-time)
pip install pip-audit

# 1. No dependency conflicts
pip check
# Expected: No broken requirements found.

# 2. No known CVEs
pip-audit
# Expected: No known vulnerabilities found
# Expected skips (not on PyPI, cannot be audited):
#   flatbuffers  — piwheels version label (20181003210633)
#   torch        — local version label (2.11.0+cpu)
#   torchaudio   — local version label (2.11.0+cpu)

# 3. No unexpected packages — compare against this file
pip list --format=freeze | cut -d= -f1 | sort
# Review: every package should be either profiled above or a transitive dep
# of a profiled package. Anything unrecognised is suspect — investigate or remove.

# 4. No stray dotenv package (known typosquat risk)
pip list | grep -i dotenv
# Expected: only python-dotenv, NOT dotenv

# 5. pip itself is current (CVEs in pip 25.x were fixed by upgrading to 26.0+)
pip --version
# Expected: pip 26.0.1 or later
```
