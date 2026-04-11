# 2-Mic HAT MVP

Hardware: **Seeed ReSpeaker 2-Mics Pi HAT** (WM8960 codec, I2S).

## Hardware summary

| Feature | Detail |
|---|---|
| Codec | WM8960 (I2S, stereo in + stereo out) |
| ALSA card | `seeed2micvoicec` (card 3, `hw:3,0`) |
| PyAudio index | 1 (`in=2 out=2`) |
| Capture | 2-ch stereo, 16 kHz int16 confirmed |
| Mixer | `Capture` volume (0-63, default 39/62%), `Left/Right Input Boost Mixer LINPUT1/RINPUT1` (+29 dB) |
| LEDs | 3× APA102 RGB, SPI bus 0 device 1 (`/dev/spidev0.1`) |
| Speaker out | Onboard headphone/speaker amp on same card (not used — playback goes to bcm2835 `hw:0,0`) |

## Smoke tests

### `smoke_capture.py` — audio recording

Records 5 s stereo via WM8960, shows per-channel RMS, saves WAV to `/tmp/`, plays back mono downmix through `hw:0,0`.

```bash
source ~/venv/bin/activate
python mvp-modules/2-mic/smoke_capture.py
```

Result: clean capture. Playback via `aplay -D hw:0,0 <wav>` is cleaner than the PyAudio playback path (known PortAudio tearing issue on bcm2835).

### `smoke_leds.py` — APA102 LED output

Drives 3 LEDs through red → green → blue → white → RGB cycle → off.

```bash
sudo python mvp-modules/2-mic/smoke_leds.py
```

**Requires root or `spi` group membership.** `/dev/spidev0.1` is `root:spi 660`. The `voice` user is not currently in the `spi` group.

Fix: `sudo usermod -aG spi voice` (then re-login or newgrp).

## Dependencies

- `spidev` — APA102 LED control over SPI (added to venv)
- `pyaudio` — already in venv

## Key differences from 4-Mic HAT (AC108)

| | 4-Mic (AC108) | 2-Mic (WM8960) |
|---|---|---|
| Capture channels | 1 (mono, from 4-mic hardware mix) | 2 (native stereo, one per mic) |
| Codec driver | AC108 (known `scheduling while atomic` bug) | WM8960 (stable, mainline) |
| Gain control | `ADC1 PGA gain` (0-28) | `Capture` (0-63) + input boost |
| LEDs | 12× APA102 (SPI) | 3× APA102 (SPI) |
| Playback on HAT | No | Yes (headphone + speaker amp) |

## Mono downmix strategy

The full pipeline (OWW → Silero VAD → SHM ring → Deepgram STT) requires mono 16 kHz int16.
The 2-mic HAT captures natively in stereo (one channel per mic).

**Approach: ALSA plug auto-downmix (no application code change).**

The recorder opens `audio_in_channels=1` via Pipecat → PyAudio. PyAudio requests
mono from ALSA, and the `plug` layer wrapping the `dsnoop` capture source in
`/etc/asound.conf` averages L+R to produce mono. This gives ~3 dB SNR gain over
a single mic and covers both mic positions spatially.

The WM8960 also exposes `ADC Data Output Select` (register R21, ADCTL1 bits 2:1)
which can clone one mic to both channels at the hardware level — useful if one mic
proves noisier and you want to lock to the other. Not needed at present.

## Resolved items

- **SPI permission**: `voice` and `agent` added to `spi` group — LED access works without root.
- **Device index TODOs**: added to `assistant/recorder_process.py` and `assistant/tts_backends.py` for future env-var configurability.
- **Mono channel contract**: `audio_in_channels=1` now explicit in recorder transport params (was implicit via Pipecat default). ALSA plug handles stereo→mono downmix.
