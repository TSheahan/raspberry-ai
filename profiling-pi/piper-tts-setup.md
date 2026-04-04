# Piper TTS Setup

Installs the Piper TTS engine and voice model used by `forked_assistant/src/master.py`
(step 8) to synthesise agent responses for playback through the bcm2835 headphone
output. Applies to the `~/pipecat-agent/venv/` virtual environment on morpheus.

---

## Target State

A satisfactorily provisioned system exhibits **all** of the following:

- `piper-tts` is installed in `~/pipecat-agent/venv/`
- `~/piper-models/en_US-lessac-medium.onnx` and its `.json` sidecar are present
- `PIPER_MODEL_PATH` is set in `~/.env` pointing to the `.onnx` file
- A smoke test synthesises a sentence and plays it through device 0 without error

---

## Install

### 1. Activate the venv

```bash
source ~/pipecat-agent/venv/bin/activate
```

### 2. Install `piper-tts`

```bash
pip install piper-tts
```

The cooldown wrapper in `/etc/profile.d/pip-cooldown.sh` applies automatically —
this rejects packages uploaded within the last seven days. `piper-tts` is a
mature package (well past the cooldown window) so no bypass is needed.

### 3. Download the voice model

```bash
mkdir -p ~/piper-models
cd ~/piper-models

# ONNX model
curl -L -o en_US-lessac-medium.onnx \
  https://github.com/rhasspy/piper/releases/download/v1.2.0/en_US-lessac-medium.onnx

# Config sidecar (required alongside the .onnx)
curl -L -o en_US-lessac-medium.onnx.json \
  https://github.com/rhasspy/piper/releases/download/v1.2.0/en_US-lessac-medium.onnx.json
```

If the v1.2.0 URLs are stale, browse
`https://github.com/rhasspy/piper/releases` for the current release and update
the paths above. The `.onnx` and `.onnx.json` must be the same release version.

### 4. Add `PIPER_MODEL_PATH` to `.env`

```bash
echo 'PIPER_MODEL_PATH=/home/user/piper-models/en_US-lessac-medium.onnx' >> ~/.env
```

---

## Verify

```bash
source ~/pipecat-agent/venv/bin/activate

# 1. Package installed
python -c "import piper; print('piper-tts ok')"
# Expected: piper-tts ok

# 2. Model loads and reports correct sample rate
python -c "
from piper.voice import PiperVoice
v = PiperVoice.load('/home/user/piper-models/en_US-lessac-medium.onnx')
print('sample_rate:', v.config.sample_rate)
"
# Expected: sample_rate: 22050

# 3. Synthesise a sentence and play through bcm2835 headphones (device 0)
python - <<'EOF'
from piper.voice import PiperVoice
import pyaudio, os

model = os.path.expanduser('~/piper-models/en_US-lessac-medium.onnx')
voice = PiperVoice.load(model)
pa = pyaudio.PyAudio()
stream = pa.open(
    format=pyaudio.paInt16,
    channels=1,
    rate=voice.config.sample_rate,
    output=True,
    output_device_index=0,
)
for chunk in voice.synthesize("Piper TTS is ready."):
    stream.write(chunk.audio_int16_bytes)
stream.stop_stream()
stream.close()
pa.terminate()
print("smoke test ok")
EOF
# Expected: audible speech through 3.5mm jack; "smoke test ok" printed
```

`piper-tts` 1.4.x uses `PiperVoice.synthesize()` returning `AudioChunk` values; use
`chunk.audio_int16_bytes` for PyAudio. Older docs referred to `synthesize_stream_raw`,
which is not present on current releases.

---

## Notes

- `en_US-lessac-medium` outputs S16_LE mono at 22050 Hz — this matches the
  PyAudio stream config in `master.py`.
- `piper-tts` has its own ONNX session (separate from OWW and Silero in the
  recorder child). It runs in the master process on cores 1–3. OWW is gated off
  during TTS playback by the `SET_IDLE` / `SET_WAKE_LISTEN` protocol, so the
  only concurrent ONNX load is cross-process on separate pinned cores — watch
  duty-cycle reports on first Pi validation run.
- Model file size: `.onnx` ≈ 63 MB. Ensure adequate free space in `/home/user`.
