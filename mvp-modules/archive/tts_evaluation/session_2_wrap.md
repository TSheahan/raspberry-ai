# TTS Evaluation — Session 2 Summary (2026-04-05)

## Primary Finding: PyAudio Tearing Root Cause Confirmed

PortAudio's internal callback thread is the cause of audio tearing on bcm2835
(Pi 4 headphone jack). Both direct ALSA (`pyalsaaudio`) and subprocess `aplay`
play the same PCM data cleanly. PyAudio tears at every `frames_per_buffer` value
tested (256–8192), though severity decreases at larger values.

**Root cause mechanism:** PortAudio interposes a background thread between the
user's `write()` call and ALSA hardware, even for "blocking" writes. When that
thread gets descheduled on the Pi 4's ARM cores (by Python GC, kernel scheduling,
or any other preemption), the hardware DMA buffer drains and produces an audible
glitch. No buffer size eliminates this because the stalls are scheduling-driven.

**Fix:** Replace PyAudio with `pyalsaaudio` (calls `snd_pcm_writei()` directly
from the calling thread, matching what `aplay` does). Applied to both `tts.py`
(production) and `compare_tts.py` (evaluation harness).

---

## Diagnostic Journey

### Step 1 — frames_per_buffer sweep

Built `replay_wav.py` to replay `deepgram_00.wav` through PyAudio with
configurable `frames_per_buffer`. Sweep results:

| fpb | Tearing (subjective 0–10) | Notes |
|-----|---------------------------|-------|
| 256 | 10 | Severe |
| 512 | 8 | |
| 1024 | 6 | |
| 2048 | 3 | |
| 4096 | 2 | Truncated playback (drain bug) |
| 8192 | 2 | Truncated playback (drain bug) |

Truncation at 4096/8192: `write()` returned before hardware finished (data queued
into large PortAudio buffer, `stop_stream()` cut the DMA tail). Fixed with a drain
wait after `write()`. After fix, tearing scores unchanged — 4096/8192 still ~2/10.

**Conclusion:** `frames_per_buffer` reduces but cannot eliminate tearing. Not root cause.

### Step 2 — Three-way backend comparison

Added `--alsaaudio` and `--aplay` modes to `replay_wav.py`. Results:

| Mode | Result |
|------|--------|
| `--aplay` (subprocess aplay -D hw:0,0) | Clean, consistent |
| `--alsaaudio` (pyalsaaudio direct ALSA) | Clean, consistent |
| `--frames 4096` (PyAudio) | Tearing present |

Multiple cycles of each showed high consistency. PortAudio confirmed as sole source.

---

## Code Changes

### `replay_wav.py` (new)
- Three-mode WAV replay: PyAudio sweep, pyalsaaudio, aplay subprocess
- Per-write() timing with underrun detection in `--chunk` mode
- Drain wait after write() to prevent stop_stream() truncation
- Summary aggregation printed after sweep

### `compare_tts.py` (updated)
- Replaced `pyaudio.PyAudio` + `_open_stream()` with `_AudioOut` abstraction
- Linux: pyalsaaudio direct ALSA to `hw:0,0`; Windows: PyAudio fallback
- Removed `pa` parameter threading through all backend functions
- Removed `--frames-per-buffer` arg (no longer relevant)

### `tts.py` (updated)
- Added `_AudioOut` class (same pattern as compare_tts.py)
- All four `TTSBackend` implementations (`DeepgramTTS`, `ElevenLabsTTS`,
  `CartesiaTTS`, `PiperTTS`) now use `_AudioOut` instead of PyAudio directly
- `close()` no longer manages `pyaudio.PyAudio` lifecycle — output is per-turn
- Module docstring updated to document the PortAudio rejection

### `profiling-pi/venv.md` (new)
- pyalsaaudio dependency profile: target state, install, verify

### `profiling-pi/AGENTS.md` (new)
- Declarative profiling approach: three-section pattern, separation of concerns

### `AGENTS.md` (updated)
- Interface contract: PyAudio → pyalsaaudio
- Phase 1 status: re-run needed with clean audio output
- File layout: added replay_wav.py, session wraps
- Hardware context: audio output line updated

### `effort_log.md` (updated)
- Phase 1 results table (from session 1 Pi run)
- Audio tearing investigation section with root cause, evidence, fix

---

## What's Next for Session 3

1. **Install pyalsaaudio on Pi** — `profiling-pi/venv.md` has the steps
2. **Re-run Phase 1** — `compare_tts.py --deepgram-only` with pyalsaaudio output
   - Confirm clean audio (no tearing)
   - Latency numbers expected ~same (2613ms avg — API-bound, not output-bound)
   - Sign off audio quality → MARGINAL on latency → proceed to Phase 2a
3. **Phase 2a — ElevenLabs** — streaming (~75ms first chunk vs Deepgram REST ~2.6s)
   - `pip install elevenlabs` in Pi venv
   - Add `ELEVENLABS_API_KEY` to `.env` on Pi
   - `compare_tts.py --elevenlabs-only`
4. **Phase 3** — integrated test with selected backend in master.py

---

## Commits This Session

| Hash | Description |
|------|-------------|
| 110f454 | Add PyAudio tearing diagnostics (replay_wav.py, compare_tts.py --frames-per-buffer) |
| ef4c513 | Fix write() truncation; aggregate sweep summary |
| 41bf72b | Add --alsaaudio and --aplay modes to replay_wav.py |
| (pending) | Replace PyAudio with pyalsaaudio; state uplift across all docs |
