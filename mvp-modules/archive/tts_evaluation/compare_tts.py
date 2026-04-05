#!/usr/bin/env python3
"""
compare_tts.py — Side-by-side TTS latency comparison: Deepgram vs ElevenLabs vs Cartesia.

Plays each test sentence through each configured backend in sequence and prints
latency and quality metrics for the effort_log.md Phase 1/2 tables.

Run on the Pi (from repo root):
    source ~/pipecat-agent/venv/bin/activate
    python mvp-modules/archive/tts_evaluation/compare_tts.py

Backends enabled by environment (loaded from .env via find_dotenv search):
    Deepgram    — DEEPGRAM_API_KEY     (always available; same key as STT)
    ElevenLabs  — ELEVENLABS_API_KEY + `pip install elevenlabs`  (Phase 2a)
    Cartesia    — CARTESIA_API_KEY   + `pip install cartesia`     (Phase 2b, optional)

Each backend runs only if its API key is set and its package is installed.
Use --only flags to force a single backend regardless.

Audio output (Linux): pyalsaaudio (direct ALSA, no PortAudio). PyAudio tearing on
bcm2835 was confirmed in session 2 — PortAudio's callback thread causes intermittent
underruns on Pi 4 ARM. pyalsaaudio calls snd_pcm_writei() directly and plays clean.
Fallback: PyAudio on non-Linux (Windows dev).

Options:
    --deepgram-only         Run Deepgram only
    --elevenlabs-only       Run ElevenLabs only
    --cartesia-only         Run Cartesia only
    --sentence TEXT         Add a test sentence (repeatable; replaces defaults)
    --pause SECS            Pause between backends/sentences, default 2.0
    --dg-model NAME         Deepgram voice ID, default: aura-2-thalia-en
    --dg-speed FLOAT        Deepgram speed 0.7–1.5, default: 0.9
    --el-voice-id ID        ElevenLabs voice ID, default: Rachel (21m00Tcm4TlvDq8ikWAM)
    --el-model NAME         ElevenLabs model ID, default: eleven_flash_v2_5
    --cartesia-model NAME   Cartesia model ID, default: sonic-3
    --cartesia-voice-id ID  Cartesia voice UUID (required for Cartesia runs)
    --cartesia-rate HZ      Cartesia PCM sample rate, default: 22050

Measurements per sentence per backend:
    latency_ms   Time from API call start to first audio byte received
                 (Deepgram: full REST round-trip; ElevenLabs/Cartesia: first stream chunk)
    total_ms     API call start to last audio byte played through ALSA
    audio_kb     Raw PCM bytes received (sanity check / billing reference)
    rss_mb       Process RSS during synthesis (from /proc/self/status)

Audio device: ALSA hw:0,0 (bcm2835 headphones) on Linux; PyAudio default on Windows.
"""

import argparse
import os
import sys
import time
import wave
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# ---------------------------------------------------------------------------
# Bootstrap — .env and src/ import path
# ---------------------------------------------------------------------------

load_dotenv(override=True)   # searches upward from cwd; override=True matches project convention

_REPO_ROOT = Path(__file__).resolve().parents[3]   # raspberry-ai/
sys.path.insert(0, str(_REPO_ROOT / "mvp-modules" / "forked_assistant" / "src"))
from tts import _strip_markdown  # noqa: E402  (side-effect-free helper from tts.py)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SENTENCES = [
    "Hello, how can I help you today?",
    "Your CPAP therapy last night ran for seven hours and forty minutes with no events.",
    "I have added a reminder for tomorrow at nine AM to call your doctor.",
]

DEEPGRAM_SAMPLE_RATE = 24000      # Aura-2 linear16 output rate; verify on first run
ELEVENLABS_SAMPLE_RATE = 24000    # pcm_24000 format; matches Deepgram — no stream reopen needed
CARTESIA_SAMPLE_RATE = 22050      # Cartesia PCM output rate; adjust if pitch is wrong

_ALSA_DEVICE = "hw:0,0"
_ALSA_PERIOD = 4096


# ---------------------------------------------------------------------------
# System helpers
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    """Current process RSS in megabytes. Linux /proc only; returns 0.0 elsewhere."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Audio output abstraction — pyalsaaudio on Linux, PyAudio fallback on Windows
# ---------------------------------------------------------------------------

class _AudioOut:
    """Thin wrapper: write(pcm_bytes), close(). Uses pyalsaaudio on Linux."""

    def __init__(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._use_alsa = sys.platform == "linux"
        self._device = None
        self._pa = None
        self._stream = None

        if self._use_alsa:
            import alsaaudio
            self._device = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK,
                device=_ALSA_DEVICE,
                channels=1,
                rate=sample_rate,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=_ALSA_PERIOD,
            )
            logger.debug("[audio] alsaaudio opened  dev={}  rate={}  period={}",
                         _ALSA_DEVICE, sample_rate, _ALSA_PERIOD)
        else:
            import pyaudio
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=sample_rate,
                output=True,
            )
            logger.debug("[audio] PyAudio opened (Windows fallback)  rate={}", sample_rate)

    def write(self, pcm: bytes) -> None:
        if self._device is not None:
            self._device.write(pcm)
        elif self._stream is not None:
            self._stream.write(pcm)

    def close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


def _save_wav(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    """Write raw S16LE PCM bytes to a WAV file and print the aplay command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)   # S16LE = 2 bytes per sample
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    logger.info("[wav] saved {} ({:.1f} KB)  ->  aplay {}", path, len(pcm_bytes) / 1024, path)
    print(f"  aplay {path}")


# ---------------------------------------------------------------------------
# Deepgram backend
# ---------------------------------------------------------------------------

def _run_deepgram_sentence(
    text: str,
    client,
    model: str,
    speed: float,
    save_path: Path | None = None,
) -> dict:
    """Synthesise `text` via Deepgram REST and play through ALSA device 0."""
    from deepgram.core.request_options import RequestOptions  # type: ignore[import]

    opts = RequestOptions(additional_query_parameters={"speed": str(speed)})
    text = _strip_markdown(text)
    if not text:
        return {}

    rss_before = _rss_mb()
    t0 = time.monotonic()

    response = client.speak.v1.audio.generate(
        text=text,
        model=model,
        encoding="linear16",
        request_options=opts,
    )
    audio = b"".join(response)
    latency_ms = (time.monotonic() - t0) * 1000
    logger.info("[deepgram] {:>5.0f} ms  {:>6.1f} KB  {!r}", latency_ms, len(audio) / 1024, text[:60])

    if save_path:
        _save_wav(save_path, audio, DEEPGRAM_SAMPLE_RATE)

    out = _AudioOut(DEEPGRAM_SAMPLE_RATE)
    try:
        out.write(audio)
    finally:
        out.close()

    total_ms = (time.monotonic() - t0) * 1000
    rss_peak = max(rss_before, _rss_mb())

    return {
        "backend": "deepgram",
        "text": text,
        "latency_ms": latency_ms,
        "total_ms": total_ms,
        "audio_kb": len(audio) / 1024,
        "rss_mb": rss_peak,
    }


def _run_deepgram(
    sentences: list[str],
    model: str,
    speed: float,
    save_dir: Path | None = None,
    sentence_offset: int = 0,
) -> list[dict]:
    from deepgram import DeepgramClient  # type: ignore[import]

    api_key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not api_key:
        logger.error("[deepgram] DEEPGRAM_API_KEY not set — skipping")
        return []

    client = DeepgramClient(api_key=api_key)
    logger.info("[deepgram] starting  model={}  speed={}  sentences={}", model, speed, len(sentences))

    results = []
    for i, text in enumerate(sentences):
        save_path = save_dir / f"deepgram_{sentence_offset + i:02d}.wav" if save_dir else None
        row = _run_deepgram_sentence(text, client, model, speed, save_path=save_path)
        if row:
            results.append(row)
    return results


# ---------------------------------------------------------------------------
# Cartesia backend
# ---------------------------------------------------------------------------

def _run_cartesia_sentence(
    text: str,
    client,
    model: str,
    voice_id: str,
    sample_rate: int,
    save_path: Path | None = None,
) -> dict:
    """Synthesise `text` via Cartesia WebSocket and play through ALSA device 0."""
    text = _strip_markdown(text)
    if not text:
        return {}

    rss_before = _rss_mb()
    t0 = time.monotonic()
    first_chunk_ms: float | None = None
    total_bytes = 0

    pcm_buffer: list[bytes] = []
    out = _AudioOut(sample_rate)
    try:
        with client.tts.websocket_connect() as connection:
            connection.send({
                "model_id": model,
                "transcript": text,
                "voice": {"mode": "id", "id": voice_id},
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": sample_rate,
                },
            })
            for response in connection:
                logger.debug(
                    "[cartesia] ws response  type={!r}  done={!r}  audio_len={}  attrs={}",
                    getattr(response, "type", "???"),
                    getattr(response, "done", "???"),
                    len(response.audio) if getattr(response, "audio", None) else 0,
                    [a for a in dir(response) if not a.startswith("_")],
                )
                if response.type == "chunk" and response.audio:
                    if first_chunk_ms is None:
                        first_chunk_ms = (time.monotonic() - t0) * 1000
                        logger.info("[cartesia] first chunk in {:.0f} ms", first_chunk_ms)
                    out.write(response.audio)
                    total_bytes += len(response.audio)
                    if save_path:
                        pcm_buffer.append(response.audio)
                elif response.type == "done":
                    break
    finally:
        out.close()

    if save_path and pcm_buffer:
        _save_wav(save_path, b"".join(pcm_buffer), sample_rate)

    total_ms = (time.monotonic() - t0) * 1000
    rss_peak = max(rss_before, _rss_mb())
    logger.info(
        "[cartesia] {:>5.0f} ms first  {:>5.0f} ms total  {:>6.1f} KB  {!r}",
        first_chunk_ms or 0, total_ms, total_bytes / 1024, text[:60],
    )

    return {
        "backend": "cartesia",
        "text": text,
        "latency_ms": first_chunk_ms or 0.0,
        "total_ms": total_ms,
        "audio_kb": total_bytes / 1024,
        "rss_mb": rss_peak,
    }


def _run_cartesia(
    sentences: list[str],
    model: str,
    voice_id: str,
    sample_rate: int,
    save_dir: Path | None = None,
    sentence_offset: int = 0,
) -> list[dict]:
    try:
        from cartesia import Cartesia  # type: ignore[import]
    except ImportError:
        logger.error("[cartesia] `cartesia` package not installed — run: pip install cartesia")
        return []

    api_key = os.environ.get("CARTESIA_API_KEY", "")
    if not api_key:
        logger.error("[cartesia] CARTESIA_API_KEY not set in .env — skipping")
        return []
    if not voice_id:
        logger.error("[cartesia] --cartesia-voice-id required — find one at https://play.cartesia.ai/voices")
        return []

    client = Cartesia(api_key=api_key)
    logger.info("[cartesia] starting  model={}  voice={}  sentences={}", model, voice_id, len(sentences))

    results = []
    for i, text in enumerate(sentences):
        save_path = save_dir / f"cartesia_{sentence_offset + i:02d}.wav" if save_dir else None
        row = _run_cartesia_sentence(text, client, model, voice_id, sample_rate, save_path=save_path)
        if row:
            results.append(row)
    return results


# ---------------------------------------------------------------------------
# ElevenLabs backend
# ---------------------------------------------------------------------------

def _run_elevenlabs_sentence(
    text: str,
    client,
    voice_id: str,
    model: str,
    save_path: Path | None = None,
) -> dict:
    """Synthesise `text` via ElevenLabs streaming and play through ALSA device 0."""
    text = _strip_markdown(text)
    if not text:
        return {}

    rss_before = _rss_mb()
    t0 = time.monotonic()
    first_chunk_ms: float | None = None
    total_bytes = 0

    pcm_buffer: list[bytes] = []
    out = _AudioOut(ELEVENLABS_SAMPLE_RATE)
    try:
        audio_stream = client.text_to_speech.stream(
            voice_id=voice_id,
            text=text,
            model_id=model,
            output_format="pcm_24000",
        )
        for pcm_chunk in audio_stream:
            if pcm_chunk:
                if first_chunk_ms is None:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                    logger.info("[elevenlabs] first chunk in {:.0f} ms", first_chunk_ms)
                out.write(pcm_chunk)
                total_bytes += len(pcm_chunk)
                if save_path:
                    pcm_buffer.append(pcm_chunk)
    finally:
        out.close()

    if save_path and pcm_buffer:
        _save_wav(save_path, b"".join(pcm_buffer), ELEVENLABS_SAMPLE_RATE)

    total_ms = (time.monotonic() - t0) * 1000
    rss_peak = max(rss_before, _rss_mb())
    logger.info(
        "[elevenlabs] {:>5.0f} ms first  {:>5.0f} ms total  {:>6.1f} KB  {!r}",
        first_chunk_ms or 0, total_ms, total_bytes / 1024, text[:60],
    )

    return {
        "backend": "elevenlabs",
        "text": text,
        "latency_ms": first_chunk_ms or 0.0,
        "total_ms": total_ms,
        "audio_kb": total_bytes / 1024,
        "rss_mb": rss_peak,
    }


def _run_elevenlabs(
    sentences: list[str],
    voice_id: str,
    model: str,
    save_dir: Path | None = None,
    sentence_offset: int = 0,
) -> list[dict]:
    try:
        from elevenlabs.client import ElevenLabs  # type: ignore[import]
    except ImportError:
        logger.error("[elevenlabs] `elevenlabs` package not installed — run: pip install elevenlabs")
        return []

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        logger.error("[elevenlabs] ELEVENLABS_API_KEY not set in .env — skipping")
        return []

    client = ElevenLabs(api_key=api_key)
    logger.info("[elevenlabs] starting  model={}  voice={}  sentences={}", model, voice_id, len(sentences))

    results = []
    for i, text in enumerate(sentences):
        save_path = save_dir / f"elevenlabs_{sentence_offset + i:02d}.wav" if save_dir else None
        row = _run_elevenlabs_sentence(text, client, voice_id, model, save_path=save_path)
        if row:
            results.append(row)
    return results


# ---------------------------------------------------------------------------
# Results display
# ---------------------------------------------------------------------------

def _print_table(results: list[dict]) -> None:
    if not results:
        print("\nNo results.")
        return

    col_w = 52
    print()
    print(f"{'Backend':<12}  {'Latency':>9}  {'Total':>9}  {'Audio':>8}  {'RSS':>7}  Text")
    print("-" * 12 + "  " + "-" * 9 + "  " + "-" * 9 + "  " + "-" * 8 + "  " + "-" * 7 + "  " + "-" * col_w)
    for r in results:
        snippet = r["text"][:col_w]
        print(
            f"{r['backend']:<12}  "
            f"{r['latency_ms']:>7.0f}ms  "
            f"{r['total_ms']:>7.0f}ms  "
            f"{r['audio_kb']:>6.1f}KB  "
            f"{r['rss_mb']:>5.0f}MB  "
            f"{snippet}"
        )

    backends = sorted({r["backend"] for r in results})
    if len(backends) < 2:
        return

    print()
    print("Summary by backend:")
    for b in backends:
        rows = [r for r in results if r["backend"] == b]
        avg_lat = sum(r["latency_ms"] for r in rows) / len(rows)
        avg_tot = sum(r["total_ms"] for r in rows) / len(rows)
        avg_rss = sum(r["rss_mb"] for r in rows) / len(rows)
        print(f"  {b:<12}  avg latency {avg_lat:.0f}ms  avg total {avg_tot:.0f}ms  avg RSS {avg_rss:.0f}MB")


def _print_effort_log_row(results: list[dict]) -> None:
    """Print a pre-filled effort_log.md Phase table row for copy-paste."""
    if not results:
        return
    print()
    print("--- effort_log.md fill-in -------------------------------------------")
    for b in sorted({r["backend"] for r in results}):
        rows = [r for r in results if r["backend"] == b]
        avg_lat = sum(r["latency_ms"] for r in rows) / len(rows)
        avg_rss = sum(r["rss_mb"] for r in rows) / len(rows)
        print(f"Backend: {b}")
        print(f"  API call -> first audio : {avg_lat:.0f} ms  (avg over {len(rows)} sentences)")
        print(f"  RSS during synthesis    : {avg_rss:.0f} MB")
        print(f"  Audio quality           : [fill in - subjective]")
        print(f"  OOM?                    : [fill in]")
        if b == "deepgram":
            gate = "PASS -> proceed to Phase 3" if avg_lat <= 800 else "MARGINAL -> proceed to Phase 2"
            print(f"  Proceed decision        : {gate}  (threshold 800ms)")
    print("-------------------------------------------------------------")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare Deepgram vs ElevenLabs vs Cartesia TTS latency on the Pi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    only = p.add_mutually_exclusive_group()
    only.add_argument("--deepgram-only", action="store_true", help="Run Deepgram only")
    only.add_argument("--elevenlabs-only", action="store_true", help="Run ElevenLabs only")
    only.add_argument("--cartesia-only", action="store_true", help="Run Cartesia only")
    p.add_argument("--sentence", dest="sentences", action="append", metavar="TEXT",
                   help="Test sentence (repeatable; replaces defaults)")
    p.add_argument("--save-wav", metavar="DIR", nargs="?", const=".",
                   help="Save each sentence as a WAV file for aplay diagnostics "
                        "(default dir: current directory)")
    p.add_argument("--pause", type=float, default=2.0, metavar="SECS",
                   help="Pause between backends within a sentence (default: 2.0)")
    p.add_argument("--dg-model", default="aura-2-thalia-en", metavar="NAME",
                   help="Deepgram voice ID (default: aura-2-thalia-en)")
    p.add_argument("--dg-speed", type=float, default=0.9, metavar="FLOAT",
                   help="Deepgram speed 0.7–1.5 (default: 0.9)")
    p.add_argument("--el-voice-id", default="21m00Tcm4TlvDq8ikWAM", metavar="ID",
                   help="ElevenLabs voice ID (default: Rachel — 21m00Tcm4TlvDq8ikWAM)")
    p.add_argument("--el-model", default="eleven_flash_v2_5", metavar="NAME",
                   help="ElevenLabs model ID (default: eleven_flash_v2_5)")
    p.add_argument("--cartesia-model", default="sonic-3", metavar="NAME",
                   help="Cartesia model ID (default: sonic-3)")
    p.add_argument("--cartesia-voice-id", default="", metavar="UUID",
                   help="Cartesia voice UUID — find one at https://play.cartesia.ai/voices")
    p.add_argument("--cartesia-rate", type=int, default=CARTESIA_SAMPLE_RATE, metavar="HZ",
                   help=f"Cartesia PCM sample rate (default: {CARTESIA_SAMPLE_RATE})")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    sentences = args.sentences or DEFAULT_SENTENCES

    run_deepgram = args.deepgram_only or not (args.elevenlabs_only or args.cartesia_only)
    run_elevenlabs = args.elevenlabs_only or not (args.deepgram_only or args.cartesia_only)
    run_cartesia = args.cartesia_only or not (args.deepgram_only or args.elevenlabs_only)
    save_dir = Path(args.save_wav) if args.save_wav else None
    if save_dir:
        logger.info("[wav] saving audio to {}/", save_dir.resolve())

    logger.info(
        "TTS comparison  sentences={}  deepgram={}  elevenlabs={}  cartesia={}",
        len(sentences), run_deepgram, run_elevenlabs, run_cartesia,
    )

    all_results: list[dict] = []

    for i, text in enumerate(sentences):
        logger.info("-- sentence {} of {} ------------------------------------------", i + 1, len(sentences))
        logger.info("  {!r}", text)
        active_backends: list[str] = []

        if run_deepgram:
            rows = _run_deepgram([text], model=args.dg_model, speed=args.dg_speed, save_dir=save_dir, sentence_offset=i)
            all_results.extend(rows)
            if rows:
                active_backends.append("deepgram")

        if run_elevenlabs:
            if active_backends:
                time.sleep(args.pause)
            rows = _run_elevenlabs([text], voice_id=args.el_voice_id, model=args.el_model, save_dir=save_dir, sentence_offset=i)
            all_results.extend(rows)
            if rows:
                active_backends.append("elevenlabs")

        if run_cartesia:
            if active_backends:
                time.sleep(args.pause)
            rows = _run_cartesia(
                [text],
                model=args.cartesia_model,
                voice_id=args.cartesia_voice_id,
                sample_rate=args.cartesia_rate,
                save_dir=save_dir,
                sentence_offset=i,
            )
            all_results.extend(rows)

        if i < len(sentences) - 1:
            time.sleep(args.pause)

    _print_table(all_results)
    _print_effort_log_row(all_results)


if __name__ == "__main__":
    main()
