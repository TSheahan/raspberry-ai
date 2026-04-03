"""
log_config.py — Shared logging configuration for forked_assistant.

Registers a TRACE level (5) below DEBUG (10) for high-frequency per-frame
diagnostics. Both the master process and the recorder child call
configure_logging() independently after fork — logging state is not inherited
across the process boundary in a useful way.

Usage:
    from log_config import configure_logging, TRACE
    configure_logging()          # call once at process entry
    logger = logging.getLogger("my_module")
    logger.log(TRACE, "per-frame detail: %s", value)

LOG_LEVEL env var controls the root level. Accepts: TRACE, PERF, DEBUG,
INFO, WARNING, ERROR. Default: INFO.

  TRACE (5)  — per-frame ring writes, OWW chunk clears, duty cycle windows
  PERF  (8)  — duty cycle bookend processors composed into pipeline;
               periodic window reports emitted; all DEBUG messages visible
  DEBUG (10) — state transitions, stream ops, OWW/Silero resets, ring detail
  INFO  (20) — wake/VAD events, transcripts, latencies, shutdown summaries
"""

import logging
import os

TRACE = 5
PERF  = 8
logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(PERF,  "PERF")


def configure_logging(default_level: str = "INFO") -> None:
    """Configure root logger to stderr with a consistent format.

    Must be called once per process, at the entry point, before any logging
    calls are made. Uses force=True so re-configuration in tests is safe.
    """
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    _custom = {"TRACE": TRACE, "PERF": PERF}
    level = _custom.get(level_name, getattr(logging, level_name, logging.INFO))
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
