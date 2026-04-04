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
    CartesiaTTS     — Cartesia WebSocket streaming (Phase 2 candidate; install: pip install cartesia)

Usage (from master.py):
    tts = DeepgramTTS()               # or whichever backend is active
    tts.play(agent.run(transcript))   # blocks until all audio is played
    tts.close()                       # call once at process exit

PiperTTS note:
    PiperTTS is retained as reference code. It is not suitable for production on the
    1 GB Pi 4: the en_US-lessac-medium ONNX model (~63 MB) exhausted total swap during
    synthesis (317 MB RSS + 385 MB swap ≈ 700 MB against 900 MB total), triggering an
    OOM kill. Audio tearing was also observed. See archive/tts_evaluation/ for the
    rearchitecture effort and the DeepgramTTS evaluation notes.

DeepgramTTS note:
    Uses deepgram-sdk v6 (already in Pi venv). Requires DEEPGRAM_API_KEY in .env.
    linear16 encoding (raw S16_LE PCM) avoids any decode step on Pi.
    Per-chunk synthesis: one API call per sentence boundary chunk from agent.run().
    Evaluation: archive/tts_evaluation/effort_log.md.

CartesiaTTS note:
    Uses cartesia SDK (pip install cartesia — not yet in Pi venv as of 2026-04-05).
    Requires CARTESIA_API_KEY in .env (new account needed).
    WebSocket streaming: first audio chunk < 200ms per Cartesia documentation.
    Per-chunk: one WebSocket send per sentence chunk; audio bytes written incrementally.
    Evaluation: archive/tts_evaluation/effort_log.md Phase 2.

ElevenLabsTTS note:
    Uses elevenlabs SDK (pip install elevenlabs — not yet in Pi venv as of 2026-04-05).
    Requires ELEVENLABS_API_KEY in .env (account at elevenlabs.io; API key under Profile → API Keys).
    Streaming via convert_as_stream(); pcm_24000 encoding (raw S16LE, 24kHz — matches Deepgram rate).
    Flash v2.5 model targets ~75ms first-chunk latency per ElevenLabs documentation.
    Evaluation: archive/tts_evaluation/effort_log.md Phase 2.
"""

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

import pyaudio
from loguru import logger


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class TTSBackend(ABC):
    """Abstract base for all TTS backends.

    master.py depends only on this interface:
        tts.play(agent.run(transcript))
        tts.close()

    Any implementation that accepts an Iterator[str] of sentence-boundary-aligned
    chunks and plays audio through PyAudio device 0 (bcm2835, S16_LE, ALSA only)
    satisfies the contract. Device index, sample rate, and encoding are backend
    responsibilities — master.py does not know or care.
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
        self._pa = pyaudio.PyAudio()
        logger.info(
            "[tts] DeepgramTTS ready  model={}  sample_rate={}  speed={}",
            model, sample_rate, speed,
        )

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise each sentence chunk via Deepgram and play through device 0.

        Opens one PyAudio stream for the turn. Each chunk results in one API
        call; audio bytes are written to the stream as received. Stream closes
        after the last chunk.
        """
        stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            output=True,
            output_device_index=0,
        )
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising ({} chars): {!r}", len(chunk), chunk[:80])
                audio_bytes = self._synthesise(chunk)
                if audio_bytes:
                    stream.write(audio_bytes)
                    logger.debug("[tts] wrote {} bytes", len(audio_bytes))
        finally:
            stream.stop_stream()
            stream.close()

    def close(self) -> None:
        """Terminate the PyAudio instance. Call once at process exit."""
        self._pa.terminate()
        logger.debug("[tts] PyAudio terminated")

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
        model       — Cartesia model ID (default: sonic-english)
        voice_id    — Cartesia voice UUID; find voices at https://play.cartesia.ai/voices
        sample_rate — PCM sample rate (default: 22050 Hz)

    Evaluation status: see archive/tts_evaluation/effort_log.md Phase 2.
    Activate only if Deepgram Phase 1 latency exceeds 800ms per sentence.
    """

    _DEFAULT_MODEL = "sonic-english"
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
        self._pa = pyaudio.PyAudio()
        logger.info(
            "[tts] CartesiaTTS ready  model={}  voice={}  sample_rate={}",
            model, voice_id, sample_rate,
        )

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise each sentence chunk via Cartesia WebSocket and play through device 0.

        Opens one PyAudio stream per turn. Opens one WebSocket per chunk (Cartesia
        SDK manages the underlying connection). Audio bytes are written to PyAudio
        as they arrive from the stream — playback and synthesis overlap.
        """
        stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            output=True,
            output_device_index=0,
        )
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising ({} chars): {!r}", len(chunk), chunk[:80])
                self._synthesise_to_stream(chunk, stream)
        finally:
            stream.stop_stream()
            stream.close()

    def close(self) -> None:
        """Terminate the PyAudio instance. Call once at process exit."""
        self._pa.terminate()
        logger.debug("[tts] PyAudio terminated")

    def _synthesise_to_stream(self, text: str, stream: pyaudio.Stream) -> None:
        """Open a Cartesia WebSocket send and write audio chunks to `stream` as they arrive."""
        try:
            ws = self._client.tts.websocket_connect()
            bytes_written = 0
            for audio_chunk in ws.send(
                model_id=self._model,
                transcript=text,
                voice={"mode": "id", "id": self._voice_id},
                output_format={
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": self._sample_rate,
                },
                stream=True,
            ):
                audio = getattr(audio_chunk, "audio", None)
                if audio:
                    stream.write(audio)
                    bytes_written += len(audio)
            ws.close()
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
        self._pa = pyaudio.PyAudio()
        logger.info(
            "[tts] ElevenLabsTTS ready  model={}  voice={}  sample_rate={}",
            model, voice_id, sample_rate,
        )

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise each sentence chunk via ElevenLabs and play through device 0.

        Opens one PyAudio stream per turn. Each chunk is one convert_as_stream()
        call; PCM bytes are written to PyAudio as they arrive.
        """
        stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            output=True,
            output_device_index=0,
        )
        try:
            for chunk in text_chunks:
                chunk = _strip_markdown(chunk)
                if not chunk:
                    continue
                logger.debug("[tts] synthesising ({} chars): {!r}", len(chunk), chunk[:80])
                self._synthesise_to_stream(chunk, stream)
        finally:
            stream.stop_stream()
            stream.close()

    def close(self) -> None:
        """Terminate the PyAudio instance. Call once at process exit."""
        self._pa.terminate()
        logger.debug("[tts] PyAudio terminated")

    def _synthesise_to_stream(self, text: str, stream: pyaudio.Stream) -> None:
        """Stream PCM bytes from ElevenLabs and write to `stream` as they arrive."""
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
                    stream.write(pcm_chunk)
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
        self._pa = pyaudio.PyAudio()
        logger.info("[tts] Piper ready  sample_rate={}  model={}",
                    self._sample_rate, model_path.name)

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise and play each text chunk through ALSA device 0.

        Synthesis is disabled via stub — drain the iterator and log text only.
        Remove the stub block below to re-enable Piper synthesis.
        """
        # STUB: Piper synthesis disabled after OOM on Pi 4. Drain and log only.
        for chunk in text_chunks:
            chunk = _strip_markdown(chunk)
            if chunk:
                logger.info("[tts:piper-stub] {}", chunk)
        return

        stream = self._pa.open(  # noqa: unreachable
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            output=True,
            output_device_index=0,
        )
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
                    stream.write(pcm)
                    bytes_written += len(pcm)
                logger.debug("[tts] played {:.0f}ms of audio ({} bytes) in {:.0f}ms",
                             bytes_written / (self._sample_rate * 2) * 1000,
                             bytes_written,
                             (_monotonic() - t0) * 1000)
        finally:
            stream.stop_stream()
            stream.close()

    def close(self) -> None:
        """Terminate the PyAudio instance. Call once at process exit."""
        self._pa.terminate()
        logger.debug("[tts] PyAudio terminated")


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
