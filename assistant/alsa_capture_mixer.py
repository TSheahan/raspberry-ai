"""
ALSA capture mixer — WM8960 (Seeed 2-mic voicecard) gain for the recorder child.

Runs once before PyAudio opens capture (see ``recorder_process``).

Appliance behaviour (so you do not rely on memory or a filled ``.env``)
------------------------------------------------------------------------
* **On by default** when this module detects the WM8960 Seeed card in
  ``/proc/asound/cards``. No env vars required for normal ship configuration.
* **Appliance defaults** if you do not set level overrides: analogue input
  boost **2** and PGA capture volume **45** on both channels (Apr 2026 speech
  / headroom tuning vs. the older hot profile boost **3** / PGA **39**).
  Operator context: ``mvp-modules/signal_levels/README.md``,
  ``mvp-modules/signal_levels/context.md``, and dated session logs in that
  folder (e.g. ``session_2026-04-11_wm8960_levels.md``).
* **Other HATs** (e.g. AC108): detection does not match → this code does
  nothing.

Opt-out and overrides
---------------------
``RECORDER_ALSA_CAPTURE_MIXER`` — set to ``0``, ``off``, or ``false`` to
disable all ``amixer`` calls (e.g. lab host or custom ALSA layout).

``RECORDER_ALSA_MIXER_CARD`` — force a card id or short name (e.g.
``seeed2micvoicec`` or ``3``) instead of auto-detection.

``RECORDER_WM8960_INPUT_BOOST`` / ``RECORDER_WM8960_PGA_CAPTURE`` — integers;
either may be set alone to override only that stage.

``RECORDER_WM8960_GAIN_PRESET`` — named profile, e.g. ``speech_balanced`` or
``legacy_hot`` (see ``_PRESETS`` below). Preset wins over the appliance
numeric defaults when no explicit boost/PGA env vars are set.

Driver maintenance
------------------
Control numids are for the WM8960 Seeed driver as shown by
``amixer -c seeed2micvoicec contents``. If a kernel update reorders numids,
update the ``_WM8960_NUMID_*`` constants.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Final

from loguru import logger

# --- WM8960 Seeed: `amixer -c seeed2micvoicec contents` (Pi OS / Trixie) ---

_WM8960_NUMID_CAPTURE_VOLUME: Final = 1
_WM8960_NUMID_LEFT_LINPUT1_BOOST: Final = 9
_WM8960_NUMID_RIGHT_RINPUT1_BOOST: Final = 8

# Appliance defaults when no level env vars are set (see mvp-modules/signal_levels/).
_APPLIANCE_DEFAULT_BOOST: Final = 2
_APPLIANCE_DEFAULT_PGA: Final = 45

_PRESETS: Final[dict[str, tuple[int, int]]] = {
    "speech_balanced": (_APPLIANCE_DEFAULT_BOOST, _APPLIANCE_DEFAULT_PGA),
    "legacy_hot": (3, 39),
}

_CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]:")


def _mixer_feature_enabled() -> bool:
    raw = os.environ.get("RECORDER_ALSA_CAPTURE_MIXER", "").strip().lower()
    if not raw:
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    if raw in ("1", "on", "true", "yes", "auto"):
        return True
    logger.warning(
        "[child] RECORDER_ALSA_CAPTURE_MIXER={!r} not understood; treating as on",
        raw,
    )
    return True


def _detect_wm8960_seeed_card() -> str | None:
    """Return ALSA ``-c`` card argument if a WM8960 Seeed 2-mic card is present."""

    try:
        text = Path("/proc/asound/cards").read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("[child] could not read /proc/asound/cards: {}", e)
        return None

    for line in text.splitlines():
        m = _CARD_LINE_RE.match(line)
        if not m:
            continue
        short = m.group(2).strip()
        low_short = short.lower()
        rest = line.lower()
        if low_short == "seeed2micvoicec" or "wm8960" in rest:
            return short
    return None


def _parse_card(raw: str) -> str:
    s = raw.strip()
    if s.isdigit():
        return s
    return s


def _resolve_card() -> str | None:
    forced = os.environ.get("RECORDER_ALSA_MIXER_CARD", "").strip()
    if forced:
        return _parse_card(forced)
    return _detect_wm8960_seeed_card()


def _amixer_cset(card: str, numid: int, value_arg: str) -> bool:
    cmd = ["amixer", "-q", "-c", card, "cset", f"numid={numid}", value_arg]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[child] amixer could not run ({}): {!r}", " ".join(cmd), e)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        logger.warning("[child] amixer failed ({}): {}", " ".join(cmd), err or proc.returncode)
        return False
    return True


def apply_recorder_alsa_capture_mixers() -> None:
    """Apply WM8960 capture gain before PyAudio starts (appliance defaults on card match)."""

    if not _mixer_feature_enabled():
        logger.info("[child] ALSA capture mixer disabled (RECORDER_ALSA_CAPTURE_MIXER)")
        return

    card = _resolve_card()
    if not card:
        logger.debug(
            "[child] ALSA capture mixer skipped (no WM8960 Seeed card; "
            "set RECORDER_ALSA_MIXER_CARD to force)"
        )
        return

    boost_s = os.environ.get("RECORDER_WM8960_INPUT_BOOST", "").strip()
    pga_s = os.environ.get("RECORDER_WM8960_PGA_CAPTURE", "").strip()
    preset_raw = os.environ.get("RECORDER_WM8960_GAIN_PRESET", "").strip().lower()

    boost: int | None = None
    pga: int | None = None
    source = "appliance_defaults"

    try:
        explicit_boost = int(boost_s) if boost_s else None
        explicit_pga = int(pga_s) if pga_s else None
    except ValueError:
        logger.warning(
            "[child] invalid integer in RECORDER_WM8960_INPUT_BOOST / RECORDER_WM8960_PGA_CAPTURE"
        )
        return

    if preset_raw and (boost_s or pga_s):
        logger.warning(
            "[child] RECORDER_WM8960_GAIN_PRESET is ignored when INPUT_BOOST or PGA_CAPTURE is set"
        )

    if boost_s or pga_s:
        boost, pga = explicit_boost, explicit_pga
        source = "environment"
    elif preset_raw:
        mapped = _PRESETS.get(preset_raw)
        if not mapped:
            logger.warning(
                "[child] unknown RECORDER_WM8960_GAIN_PRESET={!r} (known: {})",
                preset_raw,
                ", ".join(sorted(_PRESETS)),
            )
            return
        boost, pga = mapped
        source = f"preset:{preset_raw}"
    else:
        boost, pga = _APPLIANCE_DEFAULT_BOOST, _APPLIANCE_DEFAULT_PGA

    if boost is not None and not 0 <= boost <= 3:
        logger.warning("[child] RECORDER_WM8960_INPUT_BOOST must be 0–3, got {}", boost)
        return
    if pga is not None and not 0 <= pga <= 63:
        logger.warning("[child] RECORDER_WM8960_PGA_CAPTURE must be 0–63, got {}", pga)
        return

    if boost is None and pga is None:
        logger.warning("[child] ALSA capture mixer: no levels to apply (internal error)")
        return

    logger.info(
        "[child] ALSA capture mixer: card={} source={} WM8960 boost={} PGA={}",
        card,
        source,
        boost if boost is not None else "(unchanged)",
        pga if pga is not None else "(unchanged)",
    )

    if boost is not None:
        s = str(boost)
        if not (
            _amixer_cset(card, _WM8960_NUMID_LEFT_LINPUT1_BOOST, s)
            and _amixer_cset(card, _WM8960_NUMID_RIGHT_RINPUT1_BOOST, s)
        ):
            return
    if pga is not None:
        pair = f"{pga},{pga}"
        if not _amixer_cset(card, _WM8960_NUMID_CAPTURE_VOLUME, pair):
            return
