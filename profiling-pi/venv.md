# Python venv — Dependency Profiles

** USER NOTE **

There is a stale decision embodied here about where to put the venv, which must be revised before execution:
- voice agent appliance runs the app as `user` from the raspberry-ai repo
- the venv provides dependencies for the app

Package installs into `~/pipecat-agent/venv/` on morpheus (Pi 4). Each section
profiles one dependency: target state, install instructions, verification.

---

## pyalsaaudio

Direct ALSA bindings for Python. Replaces PyAudio (PortAudio) for audio output
in the TTS pipeline. PortAudio's internal callback thread causes audio tearing on
bcm2835 (Pi 4 headphone jack) — confirmed in `tts_evaluation` session 2 (2026-04-05).
`pyalsaaudio` calls `snd_pcm_writei()` from the calling thread and plays clean.

### Target State

- `pyalsaaudio` is installed in `~/pipecat-agent/venv/`
- `import alsaaudio` succeeds
- ALSA development headers (`libasound2-dev`) are present (build dependency)

### Install

```bash
source ~/pipecat-agent/venv/bin/activate

# Build dependency — pyalsaaudio compiles a C extension against libasound
sudo apt install -y libasound2-dev

pip install pyalsaaudio
```

The cooldown wrapper in `/etc/profile.d/pip-cooldown.sh` applies automatically.
`pyalsaaudio` is a mature package (well past the cooldown window).

### Verify

```bash
source ~/pipecat-agent/venv/bin/activate

# 1. Package installed
python -c "import alsaaudio; print('pyalsaaudio', alsaaudio.__version__ if hasattr(alsaaudio, '__version__') else 'ok')"
# Expected: pyalsaaudio <version> or pyalsaaudio ok

# 2. Can open hw:0,0 for playback (bcm2835 headphones)
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
print('ALSA hw:0,0 opened and closed ok')
"
# Expected: ALSA hw:0,0 opened and closed ok

# 3. Replay test (requires a WAV file — e.g. from compare_tts.py --save-wav)
# python mvp-modules/archive/tts_evaluation/replay_wav.py /tmp/tts_wav/deepgram_00.wav --alsaaudio
# Expected: clean audio through 3.5mm jack, no tearing
```

### Notes

- `pyalsaaudio` is Linux-only. On Windows (Cursor dev), the codebase falls back
  to PyAudio automatically (see `_AudioOut` in `tts.py` and `compare_tts.py`).
- The ALSA device `hw:0,0` bypasses dmix and plugin layers. This is correct for
  the Pi 4 where master.py is the sole audio output consumer.
- Period size 4096 at 24kHz = ~170ms periods. This provides comfortable headroom
  against scheduling jitter while keeping latency acceptable for voice output.
