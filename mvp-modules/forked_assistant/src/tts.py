"""
tts.py — Piper TTS integration for forked_assistant (EU-7 / step 8).

Wraps the piper-tts Python API to synthesise agent response text and play it
through the bcm2835 headphone output (PyAudio device 0, S16_LE, 22050 Hz).

Usage (from master.py):
    tts = PiperTTS(Path(os.environ["PIPER_MODEL_PATH"]))
    tts.play(agent.run(transcript))   # blocks until all audio is played
    tts.close()                       # call once at process exit

Architecture note:
    PiperVoice.synthesize() (piper-tts 1.4.x) accepts a text string and yields
    AudioChunk objects. Each chunk's audio_int16_bytes is written directly to the
    PyAudio output stream. play() opens one stream for the full turn and closes
    it when the iterator is exhausted.

    OWW is gated off during TTS playback: master sends SET_IDLE before the
    cognitive loop (which includes TTS) and SET_WAKE_LISTEN only after it
    returns. No additional barge-in guard is needed.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pyaudio
from loguru import logger
from piper.voice import PiperVoice


class PiperTTS:
    """Synthesise and play streamed text chunks via Piper + PyAudio.

    Loads the ONNX model once at construction. Each call to play() opens one
    PyAudio output stream, synthesises all chunks, and closes the stream. The
    PyAudio instance is reused across calls and terminated only on close().
    """

    def __init__(self, model_path: Path) -> None:
        model_path = Path(os.path.expanduser(str(model_path)))
        logger.info("[tts] loading Piper model: {}", model_path)
        self._voice = PiperVoice.load(model_path)
        self._sample_rate: int = self._voice.config.sample_rate
        self._pa = pyaudio.PyAudio()
        logger.info("[tts] Piper ready  sample_rate={}  model={}",
                    self._sample_rate, model_path.name)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def play(self, text_chunks: Iterator[str]) -> None:
        """Synthesise and play each text chunk through ALSA device 0.

        Opens one PyAudio output stream for the full turn, writes audio bytes
        for each chunk as they are synthesised, then closes the stream. Blocks
        until all audio has been written to the hardware buffer.

        text_chunks is typically agent.run(transcript) — a generator that
        yields sentence-boundary-aligned strings as the agent responds.
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
                chunk = chunk.strip()
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

def _monotonic() -> float:
    import time
    return time.monotonic()
