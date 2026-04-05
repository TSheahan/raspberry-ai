# Voice User Setup

**Purpose:** Create the `voice` Linux user that owns and runs the voice agent
appliance. This is the first provisioning step on a fresh Pi — everything else
(venv, agent user, pip hardening) depends on this user and home directory existing.

The Pi is treated as a single-purpose appliance: `voice` exists primarily to
own and run the voice agent. A separate privileged user (e.g. `pi`) handles
provisioning and system administration.

---

## Prerequisites

- Fresh Raspberry Pi OS (Trixie / Debian 13) image booted
- Network access (for repo clone)
- `sudo` access from the initial provisioning user (e.g. the default `pi` user,
  or via serial/SSH as root during first boot)

---

## Target State

- Linux user `voice` exists with home at `/home/voice`
- `voice` is in groups: `audio`, `gpio`
- Repository checked out at `/home/voice/raspberry-ai`
- Python venv at `/home/voice/venv/` (single venv, one purpose)
- `.env` at `/home/voice/.env` with API keys (never committed)
- `master.py` runs as `voice` with no root privileges required

---

## 1. Create the `voice` User

```bash
sudo useradd -m -d /home/voice -s /bin/bash -G audio,gpio voice
sudo passwd voice
```

- `-m` — create home directory
- `-G audio,gpio` — audio for ALSA device access, gpio for hardware

---

## 2. Home Directory Layout

After provisioning, `/home/voice` looks like:

```
/home/voice/
├── .env                    # API keys (DEEPGRAM, CARTESIA, ELEVENLABS, ANTHROPIC)
├── raspberry-ai/           # repo checkout
│   ├── mvp-modules/
│   │   └── forked_assistant/
│   │       └── src/
│   │           ├── master.py       # entry point
│   │           └── tts.py          # TTS backends
│   └── profiling-pi/               # provisioning docs (this file)
├── venv/                   # single Python venv for the voice agent
│   ├── bin/
│   ├── lib/
│   └── ...
└── piper-models/           # Piper ONNX models (optional, archived backend)
    ├── en_US-lessac-medium.onnx
    └── en_US-lessac-medium.onnx.json
```

No stray venvs in unrelated directories. One user, one venv, one purpose.

---

## 3. Clone the Repository

Install `gh` as the privileged user, then authenticate and clone as `voice`:

```bash
# As the privileged user (e.g. pi):
sudo apt install -y gh

# Authenticate as voice — prints a device code; open github.com/login/device
# on any browser and enter it
sudo -u voice -H gh auth login
# Select: GitHub.com → HTTPS → Login with a web browser

sudo -u voice -H gh auth setup-git
sudo -u voice -H git clone https://github.com/TSheahan/raspberry-ai /home/voice/raspberry-ai
```

---

## 4. Create the Python venv

Install the build dependency as the privileged user, then create the venv and
install packages as `voice`:

```bash
# As the privileged user (e.g. pi):
sudo apt install -y libasound2-dev      # build dep for pyalsaaudio

# As voice:
sudo -u voice -H python3 -m venv /home/voice/venv
sudo -u voice -H bash -c '
  source /home/voice/venv/bin/activate
  pip install pyalsaaudio                  # ALSA audio output (Linux-only)
  pip install python-dotenv                # .env loading
  pip install deepgram-sdk                 # STT + tertiary TTS
  pip install cartesia                     # primary TTS
  pip install elevenlabs                   # fallback TTS
'
```

After installing, proceed to `pip-hardening.md` to lock down the venv against
supply-chain attacks, then `venv.md` to verify each dependency individually.

---

## 5. Create `.env`

```bash
cat > /home/voice/.env << 'EOF'
DEEPGRAM_API_KEY=<your-key>
CARTESIA_API_KEY=<your-key>
ELEVENLABS_API_KEY=<your-key>
ANTHROPIC_API_KEY=<your-key>
AGENT_USER=agent
AGENT_BIN=/home/agent/.local/bin/agent
AGENT_WORKSPACE=/home/agent/personal
EOF

chmod 600 /home/voice/.env
```

---

## 6. Verify

```bash
sudo -iu voice
source /home/voice/venv/bin/activate
cd /home/voice/raspberry-ai

# 1. User and groups
id voice
# Expected: uid=...(voice) gid=...(voice) groups=...(audio),(gpio)

# 2. Repo present
git -C /home/voice/raspberry-ai status
# Expected: On branch main, clean working tree

# 3. Venv works, core imports succeed
python -c "
import alsaaudio, dotenv, deepgram, cartesia, elevenlabs
print('all imports ok')
"
# Expected: all imports ok

# 4. TTS smoke test (end-to-end: API key + SDK + ALSA)
python mvp-modules/forked_assistant/src/tts.py -b cartesia
# Expected: audio plays through 3.5mm jack, exit 0

# 5. No root required
python mvp-modules/forked_assistant/src/tts.py -b elevenlabs "Testing as voice user"
# Expected: runs without sudo, audio plays
```

---

## Notes

- **No root privileges needed at runtime.** `master.py` requires only ALSA device
  access (`audio` group) and real-time scheduling privileges (see
  `priority-permissions.md`). `voice` has no sudo access — provisioning is
  performed by a privileged user (e.g. `pi`).
- **Single venv at `/home/voice/venv/`.** Previous deployments scattered venvs
  across `~/pipecat-agent/venv/`, `~/deepgram-benchmark-venv/`, etc. A fresh
  `voice` user starts clean.
- **Piper models** are optional. The Piper backend is archived (too
  resource-intensive for 1 GB Pi 4) but the models can be kept in
  `/home/voice/piper-models/` for testing on beefier hardware.
- **The `agent` user** (Cursor CLI subprocess) is provisioned separately — see
  `agent-user-setup.md`. It is independent of `voice` and has its own home,
  credentials, and repo checkout.
