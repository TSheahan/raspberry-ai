"""
tts.py — TTS backends for forked_assistant (step 8).

Architecture
------------
TTSBackend (abstract)
    play(Iterator[str]) -> None   — synthesise and play sentence chunks; blocks until done
    close() -> None               — release audio resources at process exit

Implementations
    PiperTTS        — local Piper ONNX (reference; unsuitable for 1 GB Pi 4 — see note)
    DeepgramTTS     — Deepgram Aura REST API (Phase 1 evaluation candidate)
    ElevenLabsTTS   — ElevenLabs streaming (Phase 2a candidate)
    CartesiaTTS     — Cartesia WebSocket streaming (Phase 2b candidate)

Audio output: pyalsaaudio (direct ALSA snd_pcm_writei) on Linux; PyAudio fallback
on Windows for dev. PortAudio (PyAudio) causes tearing on bcm2835 due to its
internal callback thread getting descheduled on Pi 4 ARM — confirmed in
tts_evaluation session 2 (2026-04-05). See _AudioOut class.

Usage (from master.py):
    tts = DeepgramTTS()               # or whichever backend is active
    tts.play(agent.run(transcript))   # blocks until all audio is played
    tts.close()                       # call once at process exit
"""

import os
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Audio output — pyalsaaudio on Linux (direct ALSA), PyAudio fallback (Windows dev)
#
# PortAudio (PyAudio) causes audio tearing on bcm2835 (Pi 4 headphone jack).
# Root cause: PortAudio's internal callback thread gets descheduled on ARM,
# causing intermittent hardware buffer underruns. pyalsaaudio calls
# snd_pcm_writei() directly from the calling thread and plays clean.
# Confirmed in tts_evaluation session 2 (2026-04-05).
# ---------------------------------------------------------------------------

_ALSA_DEVICE = "hw:0,0"
_ALSA_PERIOD = 4096


class _AudioOut:
    """Thin wrapper: write(pcm_bytes), close(). Uses pyalsaaudio on Linux."""

    def __init__(self, sample_rate: int) -> None:
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


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class TTSBackend(ABC):
    """Abstract base for all TTS backends.

    master.py depends only on this interface:
        tts.play(agent.run(transcript))
        tts.close()

    Any implementation that accepts an Iterator[str] of sentence-boundary-aligned
    chunks and plays audio through ALSA device 0 (bcm2835, S16_LE) satisfies the
    contract. Device, sample rate, and encoding are backend responsibilities —
    master.py does not know or care.

    Audio output uses pyalsaaudio (direct ALSA) on Linux; PyAudio fallback on
    Windows for dev. See _AudioOut above.
    """

    @abstractmethod
    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise and play each text chunk through ALSA device 0.

        Blocks until all audio for the turn has been played. Implementations
        should process chunks incrementally (not buffer the full response) to
        keep time-to-first-audio low.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Release audio I/O resources. Call once at process exit."""
        ...


# ---------------------------------------------------------------------------
# DeepgramTTS — active evaluation candidate
# ---------------------------------------------------------------------------

class DeepgramTTS(TTSBackend):
    """Synthesise and play streamed text chunks via Deepgram Aura REST API.

    One API call per sentence chunk. Audio is returned as linear16 PCM and
    written directly to a PyAudio output stream — no decode step required.

    Environment:
        DEEPGRAM_API_KEY  — required; same key as STT (already in .env)

    Constructor args:
        model      — Deepgram Aura-2 voice ID (default: aura-2-thalia-en)
        sample_rate — PCM sample rate returned by Deepgram (default: 24000 Hz)
        speed      — speaking rate multiplier 0.7–1.5 (default: 0.9 — slightly
                     slower for clear voice assistant delivery)

    Evaluation status: see archive/tts_evaluation/effort_log.md
    """

    _DEFAULT_MODEL = "aura-2-thalia-en"
    _DEFAULT_SAMPLE_RATE = 24000
    _DEFAULT_SPEED = 0.9

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        speed: float = _DEFAULT_SPEED,
    ) -> None:
        from deepgram import DeepgramClient  # type: ignore[import]
        api_key = os.environ["DEEPGRAM_API_KEY"]
        self._client = DeepgramClient(api_key=api_key)
        self._model = model
        self._sample_rate = sample_rate
        self._speed = speed
        logger.info(
            "[tts] DeepgramTTS ready  model={}  sample_rate={}  speed={}",
            model, sample_rate, speed,
        )

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise each sentence chunk via Deepgram and play through ALSA device 0.

        Opens one audio output per turn. Each chunk results in one API call;
        audio bytes are written to the output as received.
        """
        out = _AudioOut(self._sample_rate)
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising ({} chars): {!r}", len(chunk), chunk[:80])
                audio_bytes = self._synthesise(chunk)
                if audio_bytes:
                    out.write(audio_bytes)
                    logger.debug("[tts] wrote {} bytes", len(audio_bytes))
        finally:
            out.close()

    def close(self) -> None:
        """No persistent audio resources to release (output opened per-turn)."""
        logger.debug("[tts] DeepgramTTS closed")

    def _synthesise(self, text: str) -> bytes:
        """Call Deepgram Aura REST API and return raw linear16 PCM bytes."""
        try:
            from deepgram.core.request_options import RequestOptions  # type: ignore[import]
            opts = RequestOptions(
                additional_query_parameters={"speed": str(self._speed)}
            )
            response = self._client.speak.v1.audio.generate(
                text=text,
                model=self._model,
                encoding="linear16",
                request_options=opts,
            )
            # SDK v6: response is iterable of bytes chunks
            return b"".join(response)
        except Exception:
            logger.exception("[tts] Deepgram synthesis failed for chunk: {!r}", text[:60])
            return b""


# ---------------------------------------------------------------------------
# CartesiaTTS — Phase 2 evaluation candidate (WebSocket streaming)
# ---------------------------------------------------------------------------

class CartesiaTTS(TTSBackend):
    """Synthesise and play streamed text chunks via Cartesia WebSocket API.

    Cartesia uses WebSocket streaming — audio chunks begin arriving within
    ~200ms of the first request. Each chunk is written to the PyAudio stream
    as it arrives, so playback and synthesis overlap. This gives a lower
    time-to-first-audio than the Deepgram REST path for the same sentence.

    Prerequisites:
        pip install cartesia          (not yet in Pi venv as of 2026-04-05)
        CARTESIA_API_KEY in .env      (new account required)

    Constructor args:
        model       — Cartesia model ID (default: sonic-3)
        voice_id    — Cartesia voice UUID; find voices at https://play.cartesia.ai/voices
        sample_rate — PCM sample rate (default: 22050 Hz)

    Evaluation status: see archive/tts_evaluation/effort_log.md Phase 2.
    Activate only if Deepgram Phase 1 latency exceeds 800ms per sentence.
    """

    _DEFAULT_MODEL = "sonic-3"
    _DEFAULT_SAMPLE_RATE = 22050

    def __init__(
        self,
        voice_id: str,
        model: str = _DEFAULT_MODEL,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
    ) -> None:
        from cartesia import Cartesia  # type: ignore[import]
        api_key = os.environ["CARTESIA_API_KEY"]
        self._client = Cartesia(api_key=api_key)
        self._model = model
        self._voice_id = voice_id
        self._sample_rate = sample_rate
        logger.info(
            "[tts] CartesiaTTS ready  model={}  voice={}  sample_rate={}",
            model, voice_id, sample_rate,
        )

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise each sentence chunk via Cartesia WebSocket and play through ALSA device 0."""
        out = _AudioOut(self._sample_rate)
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising ({} chars): {!r}", len(chunk), chunk[:80])
                self._synthesise_to_output(chunk, out)
        finally:
            out.close()

    def close(self) -> None:
        """No persistent audio resources to release (output opened per-turn)."""
        logger.debug("[tts] CartesiaTTS closed")

    def _synthesise_to_output(self, text: str, out: _AudioOut) -> None:
        """Open a Cartesia WebSocket send and write audio chunks as they arrive."""
        try:
            bytes_written = 0
            with self._client.tts.websocket_connect() as connection:
                connection.send({
                    "model_id": self._model,
                    "transcript": text,
                    "voice": {"mode": "id", "id": self._voice_id},
                    "output_format": {
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": self._sample_rate,
                    },
                })
                for response in connection:
                    if response.type == "chunk" and response.audio:
                        out.write(response.audio)
                        bytes_written += len(response.audio)
                    elif response.done:
                        break
            logger.debug("[tts] cartesia wrote {} bytes", bytes_written)
        except Exception:
            logger.exception("[tts] Cartesia synthesis failed for chunk: {!r}", text[:60])


# ---------------------------------------------------------------------------
# ElevenLabsTTS — Phase 2 evaluation candidate (streaming, ~75ms first chunk)
# ---------------------------------------------------------------------------

class ElevenLabsTTS(TTSBackend):
    """Synthesise and play streamed text chunks via ElevenLabs TTS API.

    Uses convert_as_stream() which yields raw PCM bytes as they arrive.
    pcm_24000 encoding matches Deepgram's sample rate — no PyAudio reconfiguration
    needed if switching between them within a session.

    Prerequisites:
        pip install elevenlabs          (not yet in Pi venv as of 2026-04-05)
        ELEVENLABS_API_KEY in .env      (elevenlabs.io — Profile → API Keys → sk_...)

    Constructor args:
        voice_id    — ElevenLabs voice ID (default: Rachel, a warm neutral voice)
        model       — ElevenLabs model ID (default: eleven_flash_v2_5 — lowest latency)
        sample_rate — PCM sample rate; must match output_format (default: 24000)

    Evaluation status: see archive/tts_evaluation/effort_log.md Phase 2.
    Activate only if Deepgram Phase 1 latency exceeds 800ms per sentence.
    """

    _DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"   # Rachel — calm, warm, neutral
    _DEFAULT_MODEL = "eleven_flash_v2_5"           # ~75ms first-chunk latency
    _DEFAULT_SAMPLE_RATE = 24000                   # matches pcm_24000 output format

    def __init__(
        self,
        voice_id: str = _DEFAULT_VOICE_ID,
        model: str = _DEFAULT_MODEL,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
    ) -> None:
        from elevenlabs.client import ElevenLabs  # type: ignore[import]
        api_key = os.environ["ELEVENLABS_API_KEY"]
        self._client = ElevenLabs(api_key=api_key)
        self._voice_id = voice_id
        self._model = model
        self._sample_rate = sample_rate
        logger.info(
            "[tts] ElevenLabsTTS ready  model={}  voice={}  sample_rate={}",
            model, voice_id, sample_rate,
        )

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise each sentence chunk via ElevenLabs and play through ALSA device 0."""
        out = _AudioOut(self._sample_rate)
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising ({} chars): {!r}", len(chunk), chunk[:80])
                self._synthesise_to_output(chunk, out)
        finally:
            out.close()

    def close(self) -> None:
        """No persistent audio resources to release (output opened per-turn)."""
        logger.debug("[tts] ElevenLabsTTS closed")

    def _synthesise_to_output(self, text: str, out: _AudioOut) -> None:
        """Stream PCM bytes from ElevenLabs and write as they arrive."""
        try:
            audio_stream = self._client.text_to_speech.stream(
                voice_id=self._voice_id,
                text=text,
                model_id=self._model,
                output_format="pcm_24000",
            )
            bytes_written = 0
            for pcm_chunk in audio_stream:
                if pcm_chunk:
                    out.write(pcm_chunk)
                    bytes_written += len(pcm_chunk)
            logger.debug("[tts] elevenlabs wrote {} bytes", bytes_written)
        except Exception:
            logger.exception("[tts] ElevenLabs synthesis failed for chunk: {!r}", text[:60])


# ---------------------------------------------------------------------------
# PiperTTS — reference implementation (not suitable for 1 GB Pi 4)
# ---------------------------------------------------------------------------

class PiperTTS(TTSBackend):
    """Synthesise and play streamed text chunks via Piper + PyAudio.

    ARCHIVED — not suitable for production on 1 GB Pi 4.
    OOM kill observed: en_US-lessac-medium (~63 MB ONNX) exhausted total swap.
    Audio tearing was also observed before the kill.
    Retained as reference code; use DeepgramTTS or CartesiaTTS instead.

    Loads the ONNX model once at construction. Each call to play() opens one
    PyAudio output stream, synthesises all chunks, and closes the stream. The
    PyAudio instance is reused across calls and terminated only on close().
    """

    def __init__(self, model_path: Path) -> None:
        from piper.voice import PiperVoice  # type: ignore[import]
        model_path = Path(os.path.expanduser(str(model_path)))
        logger.info("[tts] loading Piper model: {}", model_path)
        self._voice = PiperVoice.load(model_path)
        self._sample_rate: int = self._voice.config.sample_rate
        logger.info("[tts] Piper ready  sample_rate={}  model={}",
                    self._sample_rate, model_path.name)

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesis disabled — drain the iterator and log text only."""
        for chunk in text_chunks:
            chunk = _strip_markdown(chunk)
            if chunk:
                logger.info("[tts:piper-stub] {}", chunk)
        return

        out = _AudioOut(self._sample_rate)  # noqa: unreachable
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising chunk ({} chars): {!r}",
                             len(chunk), chunk[:80])
                t0 = _monotonic()
                bytes_written = 0
                for audio_chunk in self._voice.synthesize(chunk):
                    pcm = audio_chunk.audio_int16_bytes
                    out.write(pcm)
                    bytes_written += len(pcm)
                logger.debug("[tts] played {:.0f}ms of audio ({} bytes) in {:.0f}ms",
                             bytes_written / (self._sample_rate * 2) * 1000,
                             bytes_written,
                             (_monotonic() - t0) * 1000)
        finally:
            out.close()

    def close(self) -> None:
        """No persistent audio resources to release."""
        logger.debug("[tts] PiperTTS closed")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """Remove markdown syntax that TTS backends would read as literal characters.

    Handles the common patterns in agent responses: bold/italic markers, headers,
    list bullets, and inline code spans. Preserves underlying words.
    """
    # Bold/italic: **text**, *text*, __text__, _text_
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_', r'\1', text, flags=re.DOTALL)
    # ATX headers: "## Heading"
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Numbered list markers: "1. " at line start
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    # Bullet list markers: "- " or "* " at line start
    text = re.sub(r'^[-*]\s+', '', text, flags=re.MULTILINE)
    # Inline code: `code`
    text = re.sub(r'`([^`]+)`', r'\1', text)
    return text.strip()


def _monotonic() -> float:
    import time
    return time.monotonic()
