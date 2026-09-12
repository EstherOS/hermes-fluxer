"""Fluxer voice lane (spec §6-W3) — lazy exports only.

Importing this package must **never** import ``livekit`` and must never raise:
a venv without livekit keeps the plugin text-only.  LiveKit is imported inside
functions via :func:`try_livekit`, which logs the actionable install hint
exactly once:

    voice disabled: install livekit into the Hermes venv

Public names (``VoiceConfig``, ``parse_voice_config``, ``VoiceController``,
``VoiceCascade``, ``audio``, ``try_livekit``) are resolved on first attribute
access so import order stays cycle-free.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

log = logging.getLogger("fluxer.voice")

__all__ = [
    "VoiceConfig", "parse_voice_config", "VoiceController", "VoiceCascade",
    "OmniVoiceBridge",
    "audio", "try_livekit", "livekit_available",
]

_LAZY = {
    # Relative to THIS package — absolute "fluxer.*" targets break under the
    # gateway loader (hermes_plugins.<slug>) and the validate probe; resolve
    # via __getattr__ below with package=__name__.
    "VoiceConfig": "config",
    "parse_voice_config": "config",
    "VoiceController": "controller",
    "VoiceCascade": "cascade",
    "OmniVoiceBridge": "omni_bridge",
    "audio": "audio",
}

_livekit: Any = None
_livekit_checked = False
_livekit_missing_logged = False
#: The one-line install hint (c4a report §6); asserted by tests.
LIVEKIT_MISSING_MESSAGE = (
    "Fluxer voice disabled: install livekit into the Hermes venv "
    "(`uv pip install livekit`) — text-only mode"
)


def try_livekit() -> Optional[Any]:
    """Return the ``livekit.rtc`` module or None (logs the install hint once)."""
    global _livekit, _livekit_checked, _livekit_missing_logged
    if _livekit_checked:
        return _livekit
    _livekit_checked = True
    try:
        from livekit import rtc  # noqa: PLC0415 — intentional lazy import
        _livekit = rtc
    except Exception as e:  # ImportError, native-FFI load failures, …
        _livekit = None
        if not _livekit_missing_logged:
            _livekit_missing_logged = True
            log.warning("%s (%s: %s)", LIVEKIT_MISSING_MESSAGE, type(e).__name__, e)
    return _livekit


def livekit_available() -> bool:
    """Passive probe used by tests and the evidence scripts."""
    return try_livekit() is not None


def reset_livekit_probe() -> None:
    """Testing seam: forget a previous availability probe result."""
    global _livekit, _livekit_checked, _livekit_missing_logged
    _livekit = None
    _livekit_checked = False
    _livekit_missing_logged = False


def __getattr__(name: str) -> Any:  # PEP 562 — lazy submodule re-exports
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module("." + target, package=__name__)
    value = getattr(module, name)
    globals()[name] = value  # cache after first resolution
    return value
