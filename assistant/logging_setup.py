"""
logging_setup.py — Shared logging configuration for the voice assistant runtime.

Routes stdlib logging through loguru so our output matches Pipecat's
format and coloring exactly. Both processes call configure_logging()
independently after fork — logging state is not inherited usefully
across the process boundary.

Usage:
    from logging_setup import configure_logging, TRACE, PERF
    from loguru import logger
    configure_logging()          # call once at process entry
    logger.log(TRACE, "per-frame detail: %s", value)

LOG_LEVEL env var controls the minimum level. Accepts: TRACE, PERF, DEBUG,
INFO, WARNING, ERROR. Default: INFO.

  TRACE (5)  — per-frame ring reads/writes, OWW chunk clears
  PERF  (8)  — duty cycle periodic reports; all DEBUG visible at this level
  DEBUG (10) — state transitions, stream ops, OWW/Silero resets
  INFO  (20) — wake/VAD events, transcripts, latencies, shutdown summaries

Third-party stdlib loggers (after ``configure_logging()``):

  Both ``cartesia`` and ``websockets`` default to **DEBUG** with filters that
  truncate audio payloads: websockets BINARY frames are reduced to opcode +
  byte count; Cartesia JSON ``"data"`` blobs are replaced with a decoded-size
  placeholder via byte-offset splicing (no ``json.loads`` overhead).
  Override with ``CARTESIA_LOG_LEVEL`` / ``WEBSOCKETS_LOG_LEVEL`` (same names as
  ``logging`` levels: ``DEBUG``, ``INFO``, …).
"""

import logging
import os
import sys

from loguru import logger

TRACE = 5
PERF  = 8

# Register PERF with loguru. TRACE already exists natively at level 5.
logger.level("PERF", no=PERF, color="<magenta>", icon="⚡")

# Loguru does NOT reverse-map severity numbers back to level names.
# logger.log(8, ...) emits "Level 8"; logger.log("PERF", ...) emits "PERF   ".
# Use the integer constants (PERF, TRACE) for threshold comparisons only.
# Use the string name ("PERF", "TRACE") in all logger.log() call sites.

# Resolved numeric level — set by configure_logging(), read by callers that
# need the active threshold without re-parsing the environment.
_active_level_no: int = 20  # INFO until configure_logging() runs


def active_level_no() -> int:
    """Return the numeric log level resolved by the most recent configure_logging() call."""
    return _active_level_no


class _InterceptHandler(logging.Handler):
    """Route stdlib logging records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _format(record: dict) -> str:
    """Format callable for the loguru sink.

    Uses extra["name"] when present (set by voice_assistant.py's logger.bind call)
    so that process shows as "master" rather than "__main__". Falls back to
    the loguru module name for all other callers. Matches loguru's default
    format with {function}:{line} for call-site context.
    """
    name = record["extra"].get("name", record["name"])
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        f"<cyan>{name}</cyan>:<cyan>{{function}}</cyan>:<cyan>{{line}}</cyan>"
        " - <level>{message}</level>\n{exception}"
    )


def configure_logging(default_level: str = "INFO") -> None:
    """Configure loguru as the unified sink for both our code and Pipecat.

    Installs _InterceptHandler on the stdlib root logger so that third-party
    libraries (websockets, deepgram, asyncio) are forwarded into loguru. Our
    own source files use loguru directly. Removes loguru's default stderr sink
    and re-adds it filtered to the requested level.

    Must be called once per process, before any logging calls are made.
    """
    global _active_level_no
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    _active_level_no = logger.level(level_name).no

    # Replace loguru's default sink with one filtered to our level.
    # Loguru resolves "TRACE", "PERF", "DEBUG" etc. against its registered
    # levels (PERF was added above); raises ValueError for unknown names.
    # colorize=None: auto-detect TTY (colors in terminal, plain when piped).
    logger.remove()
    logger.add(sys.stderr, level=level_name, format=_format)

    # Route all stdlib logging into loguru. level=0 passes everything through;
    # filtering happens in the loguru sink above.
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    _apply_third_party_log_levels()


class _WebsocketsBinaryFrameFilter(logging.Filter):
    """Truncate websockets debug logs for BINARY frames (audio media chunks).

    The log call is ``logger.debug("> %s", frame)`` where ``frame`` is a
    ``websockets.frames.Frame``.  For BINARY frames, we replace the arg with
    a short summary (opcode + byte count) *before* ``Frame.__str__`` is called,
    avoiding the expensive hex-encoding of the full payload.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.DEBUG or not record.args:
            return True
        from websockets.frames import Frame, Opcode

        args = record.args if isinstance(record.args, tuple) else (record.args,)
        for i, a in enumerate(args):
            if isinstance(a, Frame) and a.opcode is Opcode.BINARY:
                record.args = tuple(
                    f"BINARY [{len(a.data)} bytes]" if j == i else v
                    for j, v in enumerate(args)
                )
                break
        return True


# Byte needles for locating the base64 audio blob in Cartesia JSON.
# The server may or may not include a space after the colon.
_DATA_NEEDLE = b'"data":"'
_DATA_NEEDLE_SP = b'"data": "'

def _truncate_cartesia_data(raw: bytes | str) -> bytes | str:
    """Replace the base64 ``"data"`` value in a Cartesia JSON message with a
    byte-length placeholder.  Pure byte-offset slicing — no JSON decode.

    Returns the original object unchanged if there is no ``"data"`` key.
    """
    b = raw if isinstance(raw, bytes) else raw.encode()

    start = b.find(_DATA_NEEDLE)
    if start != -1:
        val_start = start + len(_DATA_NEEDLE)
    else:
        start = b.find(_DATA_NEEDLE_SP)
        if start == -1:
            return raw
        val_start = start + len(_DATA_NEEDLE_SP)

    val_end = b.find(b'"', val_start)
    if val_end == -1:
        return raw

    b64_len = val_end - val_start
    audio_bytes = b64_len * 3 // 4  # base64 → decoded size (approx)
    placeholder = f"<{audio_bytes} bytes audio>".encode()
    truncated = b[:val_start] + placeholder + b[val_end:]

    return truncated if isinstance(raw, bytes) else truncated.decode()


class _CartesiaAudioDataFilter(logging.Filter):
    """Truncate the base64 ``"data"`` blob in Cartesia TTS websocket debug logs.

    The emit site is ``log.debug("Received websocket message: %s", message)``
    where ``message`` is raw JSON bytes from the wire.  We locate the
    ``"data":"<base64>"`` span with byte offsets and splice in a size
    placeholder — no ``json.loads``, so there is zero decode overhead on top
    of what ``parse_event`` already does.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.DEBUG or not record.args:
            return True
        args = record.args if isinstance(record.args, tuple) else (record.args,)
        replaced = False
        new_args: list = []
        for a in args:
            if isinstance(a, (bytes, str)) and not replaced:
                t = _truncate_cartesia_data(a)
                if t is not a:
                    replaced = True
                new_args.append(t)
            else:
                new_args.append(a)
        if replaced:
            record.args = tuple(new_args)
        return True


def _apply_third_party_log_levels() -> None:
    """Set per-library log levels and install payload filters.

    Both ``websockets`` and ``cartesia`` default to **DEBUG** with filters that
    truncate binary/audio payloads, keeping connection lifecycle and protocol
    debug output visible without hex or base64 spam.
    """

    # tracking this as a possible re-introduction
    # - first try relying on the filters and setting debug when wanted.
    # def _set(name: str, env_key: str, default: int) -> None:
    #     raw = os.environ.get(env_key, "").strip().upper()
    #     if raw:
    #         level = getattr(logging, raw, default)
    #     else:
    #         level = default
    #     logging.getLogger(name).setLevel(level)
    #_set("cartesia", "CARTESIA_LOG_LEVEL", logging.DEBUG)
    #_set("websockets", "WEBSOCKETS_LOG_LEVEL", logging.DEBUG)

    ws_filter = _WebsocketsBinaryFrameFilter()
    for name in ("websockets", "websockets.client", "websockets.server"):
        logging.getLogger(name).addFilter(ws_filter)

    cartesia_filter = _CartesiaAudioDataFilter()
    logging.getLogger("cartesia.resources.tts").addFilter(cartesia_filter)
