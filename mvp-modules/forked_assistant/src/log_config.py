"""
log_config.py — Shared logging configuration for forked_assistant.

Routes stdlib logging through loguru so our output matches Pipecat's
format and coloring exactly. Both processes call configure_logging()
independently after fork — logging state is not inherited usefully
across the process boundary.

Usage:
    from log_config import configure_logging, TRACE, PERF
    from loguru import logger
    configure_logging()          # call once at process entry
    logger.log(TRACE, "per-frame detail: %s", value)

LOG_LEVEL env var controls the minimum level. Accepts: TRACE, PERF, DEBUG,
INFO, WARNING, ERROR. Default: INFO.

  TRACE (5)  — per-frame ring reads/writes, OWW chunk clears
  PERF  (8)  — duty cycle periodic reports; all DEBUG visible at this level
  DEBUG (10) — state transitions, stream ops, OWW/Silero resets
  INFO  (20) — wake/VAD events, transcripts, latencies, shutdown summaries
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

    Uses extra["name"] when present (set by master.py's logger.bind call)
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
