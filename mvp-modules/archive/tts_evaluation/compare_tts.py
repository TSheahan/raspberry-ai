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
    --cartesia-model NAME   Cartesia model ID, default: sonic-english
    --cartesia-voice-id ID  Cartesia voice UUID (required for Cartesia runs)
    --cartesia-rate HZ      Cartesia PCM sample rate, default: 22050

Measurements per sentence per backend:
    latency_ms   Time from API call start to first audio byte received
                 (Deepgram: full REST round-trip; ElevenLabs/Cartesia: first stream chunk)
    total_ms     API call start to last audio byte played through PyAudio
    audio_kb     Raw PCM bytes received (sanity check / billing reference)
    rss_mb       Process RSS during synthesis (from /proc/self/status)

Audio device: PyAudio output_device_index=0 (bcm2835 headphones, ALSA device 0).
"""

import argparse
import os
import sys
import time
from pathlib import Path

import pyaudio
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
PYAUDIO_DEVICE_INDEX = 0 if sys.platform != "win32" else None
# Pi: device 0 = bcm2835 headphones (ALSA). Windows: None = PortAudio default output.


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


def _open_stream(pa: pyaudio.PyAudio, sample_rate: int) -> pyaudio.Stream:
    return pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=sample_rate,
        output=True,
        output_device_index=PYAUDIO_DEVICE_INDEX,
    )


# ---------------------------------------------------------------------------
# Deepgram backend
# ---------------------------------------------------------------------------

def _run_deepgram_sentence(
    text: str,
    client,
    model: str,
    speed: float,
    pa: pyaudio.PyAudio,
) -> dict:
    """Synthesise `text` via Deepgram REST and play through device 0.

    Deepgram Aura uses a REST endpoint — the full audio response is received
    before playback starts. `latency_ms` is therefore the complete API round-trip
    (network RTT + synthesis time + audio transfer). For a ~15-word sentence
    expect 200–600ms depending on network conditions.
    """
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
    audio = b"".join(response)          # REST: join iterator → full PCM bytes
    latency_ms = (time.monotonic() - t0) * 1000
    logger.info("[deepgram] {:>5.0f} ms  {:>6.1f} KB  {!r}", latency_ms, len(audio) / 1024, text[:60])

    stream = _open_stream(pa, DEEPGRAM_SAMPLE_RATE)
    try:
        stream.write(audio)
    finally:
        stream.stop_stream()
        stream.close()

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
    pa: pyaudio.PyAudio,
) -> list[dict]:
    from deepgram import DeepgramClient  # type: ignore[import]

    api_key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not api_key:
        logger.error("[deepgram] DEEPGRAM_API_KEY not set — skipping")
        return []

    client = DeepgramClient(api_key=api_key)
    logger.info("[deepgram] starting  model={}  speed={}  sentences={}", model, speed, len(sentences))

    results = []
    for text in sentences:
        row = _run_deepgram_sentence(text, client, model, speed, pa)
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
    pa: pyaudio.PyAudio,
) -> dict:
    """Synthesise `text` via Cartesia WebSocket and play through device 0.

    Cartesia uses WebSocket streaming — audio chunks arrive while synthesis
    continues. `latency_ms` is the time to the first audio chunk, which should
    be < 200ms per Cartesia documentation. Chunks are written to PyAudio as they
    arrive, so playback and synthesis overlap.

    SDK note: tested against cartesia>=1.0. The chunk object has a `.audio`
    attribute (bytes or None). Verify on first Pi run; SDK internals change.
    """
    text = _strip_markdown(text)
    if not text:
        return {}

    rss_before = _rss_mb()
    t0 = time.monotonic()
    first_chunk_ms: float | None = None
    total_bytes = 0

    stream = _open_stream(pa, sample_rate)
    ws = client.tts.websocket_connect()
    try:
        for chunk in ws.send(
            model_id=model,
            transcript=text,
            voice={"mode": "id", "id": voice_id},
            output_format={
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": sample_rate,
            },
            stream=True,
        ):
            audio = getattr(chunk, "audio", None)
            if audio:
                if first_chunk_ms is None:
                    first_chunk_ms = (time.monotonic() - t0) * 1000
                    logger.info("[cartesia] first chunk in {:.0f} ms", first_chunk_ms)
                stream.write(audio)
                total_bytes += len(audio)
    finally:
        ws.close()
        stream.stop_stream()
        stream.close()

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
    pa: pyaudio.PyAudio,
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
    for text in sentences:
        row = _run_cartesia_sentence(text, client, model, voice_id, sample_rate, pa)
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
    pa: pyaudio.PyAudio,
) -> dict:
    """Synthesise `text` via ElevenLabs convert_as_stream() and play through device 0.

    ElevenLabs streams PCM chunks as they are synthesised. `latency_ms` is the
    time to the first audio chunk. Flash v2.5 targets ~75ms per ElevenLabs docs.
    Chunks are written to PyAudio as they arrive — playback and synthesis overlap.

    SDK note: elevenlabs>=1.0. convert_as_stream() yields bytes objects.
    output_format="pcm_24000" returns raw S16LE at 24kHz — no decode needed.
    """
    text = _strip_markdown(text)
    if not text:
        return {}

    rss_before = _rss_mb()
    t0 = time.monotonic()
    first_chunk_ms: float | None = None
    total_bytes = 0

    stream = _open_stream(pa, ELEVENLABS_SAMPLE_RATE)
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
                stream.write(pcm_chunk)
                total_bytes += len(pcm_chunk)
    finally:
        stream.stop_stream()
        stream.close()

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
    pa: pyaudio.PyAudio,
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
    for text in sentences:
        row = _run_elevenlabs_sentence(text, client, voice_id, model, pa)
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
    p.add_argument("--cartesia-model", default="sonic-english", metavar="NAME",
                   help="Cartesia model ID (default: sonic-english)")
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

    logger.info(
        "TTS comparison  sentences={}  deepgram={}  elevenlabs={}  cartesia={}",
        len(sentences), run_deepgram, run_elevenlabs, run_cartesia,
    )

    pa = pyaudio.PyAudio()
    all_results: list[dict] = []

    try:
        for i, text in enumerate(sentences):
            logger.info("-- sentence {} of {} ------------------------------------------", i + 1, len(sentences))
            logger.info("  {!r}", text)
            active_backends: list[str] = []

            if run_deepgram:
                rows = _run_deepgram([text], model=args.dg_model, speed=args.dg_speed, pa=pa)
                all_results.extend(rows)
                if rows:
                    active_backends.append("deepgram")

            if run_elevenlabs:
                if active_backends:
                    time.sleep(args.pause)
                rows = _run_elevenlabs([text], voice_id=args.el_voice_id, model=args.el_model, pa=pa)
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
                    pa=pa,
                )
                all_results.extend(rows)

            if i < len(sentences) - 1:
                time.sleep(args.pause)
    finally:
        pa.terminate()

    _print_table(all_results)
    _print_effort_log_row(all_results)


if __name__ == "__main__":
    main()
