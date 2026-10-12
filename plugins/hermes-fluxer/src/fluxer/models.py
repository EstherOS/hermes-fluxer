"""Thin normalization helpers for Fluxer objects (spec ``§2.3``).

Pure functions only — no I/O, no third-party imports.  The protocol clients
(``rest.py`` / ``gatewayws.py``) and the adapter share these so that key
defaults and timestamp parsing are handled in exactly one place.

The contract is intentionally small: ensure the keys callers rely on exist,
light type coercion, ISO-8601 timestamps to :class:`datetime`.  Anything
policy-ish (filters, routing) belongs in the adapter.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

__all__ = [
    "AT_MENTION_RE",
    "parse_user",
    "parse_channel",
    "parse_message",
    "display_name",
    "message_author_id",
    "message_is_bot",
    "mentioned_user_ids",
]

#: ``<@id>`` and ``<@!id>`` mention syntax (the legacy ``!`` nickname form
#: included).  ``findall`` yields the raw user ids as strings.
AT_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a Fluxer ISO-8601 timestamp (``2026-09-11T07:53:36.954Z``)."""
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def display_name(user: Mapping[str, Any] | None) -> str:
    """Best human-readable name for a user object (never ``None``)."""
    u = user or {}
    return str(u.get("global_name") or u.get("username") or u.get("id") or "")


def parse_user(d: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a (partial) user object; extra keys are preserved."""
    user = dict(d or {})
    user["id"] = str(user.get("id") or "")
    user.setdefault("username", "")
    user.setdefault("discriminator", None)
    user.setdefault("global_name", None)
    user.setdefault("avatar", None)
    user["bot"] = bool(user.get("bot", False))
    user["display_name"] = display_name(user)
    return user


def parse_channel(d: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a channel object; ``recipients`` are parsed users.

    Adds ``recipient_ids`` for DM handling.  Channel types: 0 text,
    1 DM, 2 guild voice, 3 group DM, 4 category (see api-notes §2).
    """
    chan = dict(d or {})
    chan["id"] = str(chan.get("id") or "")
    try:
        chan["type"] = int(chan.get("type", 0))
    except (TypeError, ValueError):
        chan["type"] = 0
    for key in ("name", "guild_id", "parent_id"):
        chan.setdefault(key, None)
    recipients = [parse_user(r) for r in (chan.get("recipients") or [])]
    chan["recipients"] = recipients
    chan["recipient_ids"] = [r["id"] for r in recipients]
    return chan


def parse_message(d: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a message object.

    Ensures the keys the adapter reads exist; parses ``timestamp`` /
    ``edited_timestamp`` to :class:`datetime` and normalizes ``author``
    through :func:`parse_user`.  Extra/unknown keys pass through untouched.
    """
    msg = dict(d or {})
    msg["id"] = str(msg.get("id") or "")
    msg["channel_id"] = str(msg.get("channel_id") or "")
    msg["content"] = msg.get("content") or ""
    msg["author"] = parse_user(msg.get("author"))
    msg["timestamp"] = _parse_timestamp(msg.get("timestamp"))
    msg["edited_timestamp"] = _parse_timestamp(msg.get("edited_timestamp"))
    for key in ("mentions", "mention_roles", "embeds", "attachments", "stickers"):
        if msg.get(key) is None:
            msg[key] = []
    msg["pinned"] = bool(msg.get("pinned", False))
    msg["tts"] = bool(msg.get("tts", False))
    msg.setdefault("guild_id", None)
    msg.setdefault("channel_type", None)
    try:
        msg["type"] = int(msg.get("type", 0))
    except (TypeError, ValueError):
        msg["type"] = 0
    return msg


def message_author_id(message: Mapping[str, Any] | None) -> str | None:
    """Author id of a (raw or parsed) message, or ``None``."""
    author = (message or {}).get("author")
    author_id = author.get("id") if isinstance(author, Mapping) else None
    return str(author_id) if author_id else None


def message_is_bot(message: Mapping[str, Any] | None) -> bool:
    """True when the message author is flagged as a bot account."""
    author = (message or {}).get("author")
    return bool(author.get("bot")) if isinstance(author, Mapping) else False


def mentioned_user_ids(content: str | None) -> list[str]:
    """User ids mentioned in raw message content (``<@id>``/``<@!id>``)."""
    return AT_MENTION_RE.findall(content or "")
