"""Inbound attachment download + caching for the fluxer adapter (spec §6 W2).

Fluxer attachment mechanics (``docs/fluxer-api-notes.md`` §3): message
``attachments[]`` carry an absolute ``url`` (and ``proxy_url``) on the media
host — public reads, **no auth header** (the path is the authorization), exact
bytes, lifespan tracks the message.  The adapter downloads the bytes itself and
hands the core **local file paths**: ``MessageEvent.media_urls`` (paths) +
``media_types`` (MIME strings) with a ``message_type`` picking the class
(integration guide §6.1).

Public surface:

* :func:`download_attachment` — aiohttp GET (no auth), timeout-bounded and
  capped against ``gateway.max_inbound_media_bytes`` (the same cap the shared
  ``cache_*_from_bytes`` funnels enforce); raises :class:`AttachmentDownloadError`.
* :func:`message_type_for` — mime (+ voice-note flag) → :class:`MessageType`.
* :func:`cache_inbound_attachments` — download + cache every attachment,
  skipping failures/oversize with a log; returns
  ``(media_urls, media_types, message_type | None)``.

Voice notes: a message (or attachment) flagged with ``VOICE_MESSAGE`` (8192,
api notes §2) maps ``audio/*`` to :data:`MessageType.VOICE` (auto-STT per
integration §6.1) instead of :data:`MessageType.AUDIO`.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any, List, Optional, Tuple

import aiohttp

from gateway.platforms.base import (
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    get_inbound_media_max_bytes,
)
from gateway.platforms.event import MessageType
from gateway.platforms.media_cache import ext_for_mime

logger = logging.getLogger(__name__)

__all__ = [
    "AttachmentDownloadError",
    "VOICE_MESSAGE_FLAG",
    "download_attachment",
    "message_type_for",
    "cache_inbound_attachments",
]

#: Message/attachment flag marking a native voice note (api notes §2).
VOICE_MESSAGE_FLAG = 8192
#: Default total timeout for one attachment download.
DOWNLOAD_TIMEOUT_SECONDS = 60.0
#: Streaming read chunk size.
READ_CHUNK_BYTES = 64 * 1024

#: Media-class ranking for the per-message ``message_type`` when a message carries
#: several attachments: image > audio (voice-note > plain) > video > document.
_MESSAGE_TYPE_RANK = {
    MessageType.PHOTO: 5,
    MessageType.VOICE: 4,
    MessageType.AUDIO: 3,
    MessageType.VIDEO: 2,
    MessageType.DOCUMENT: 1,
}


class AttachmentDownloadError(Exception):
    """One attachment could not be downloaded; ``str(exc)`` is the reason.

    ``status`` carries the HTTP status when a response was received.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _normalize_mime(mime: Any) -> str:
    return str(mime or "").split(";")[0].strip().lower()


def _attachment_mime(att: dict) -> str:
    """Best MIME for an attachment: declared ``content_type``, else the filename.

    Fluxer declares ``content_type`` on the attachment object; the filename
    guess is only a fallback for missing/octet-stream values.
    """
    declared = _normalize_mime(att.get("content_type"))
    if declared and declared != "application/octet-stream":
        return declared
    filename = str(att.get("filename") or "")
    if filename:
        guessed = mimetypes.guess_type(filename)[0]
        if guessed:
            return _normalize_mime(guessed)
    return declared


def message_type_for(mime: Any, *, is_voice_note: bool = False) -> MessageType:
    """Map a MIME type to the core :class:`MessageType`.

    ``audio/*`` is :data:`MessageType.VOICE` when the voice-note flag is set
    (the STT entry point), else :data:`MessageType.AUDIO`; everything that is
    not ``image/`` / ``audio/`` / ``video/`` is a document.
    """
    primary = _normalize_mime(mime)
    if primary.startswith("image/"):
        return MessageType.PHOTO
    if primary.startswith("audio/"):
        return MessageType.VOICE if is_voice_note else MessageType.AUDIO
    if primary.startswith("video/"):
        return MessageType.VIDEO
    return MessageType.DOCUMENT


def _is_voice_note(message_flags: int, attachment_flags: int) -> bool:
    """True when the VOICE_MESSAGE flag rides the message or the attachment."""
    return bool(message_flags & VOICE_MESSAGE_FLAG) or bool(attachment_flags & VOICE_MESSAGE_FLAG)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def download_attachment(
    url: str,
    *,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    max_bytes: Optional[int] = None,
) -> bytes:
    """GET an attachment URL and return its bytes (no auth header — api notes §3/§5).

    ``max_bytes`` defaults to ``get_inbound_media_max_bytes()`` (the global
    inbound cap; 0/negative disables it) and is enforced against both a declared
    ``Content-Length`` and the running total.  Raises
    :class:`AttachmentDownloadError` with a clear reason for invalid URLs,
    non-200 responses, transport/timeout failures and oversized payloads.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise AttachmentDownloadError(f"invalid attachment url: {url!r}")
    raw_cap = get_inbound_media_max_bytes() if max_bytes is None else int(max_bytes)
    cap = max(0, raw_cap)  # 0/negative disables the cap (base semantics)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            # No Authorization header: the URL's path/query *is* the credential.
            async with session.get(url, headers={"Accept": "*/*"}) as resp:
                if resp.status != 200:
                    raise AttachmentDownloadError(
                        f"attachment download failed: HTTP {resp.status}", status=resp.status
                    )
                if cap:
                    declared = resp.headers.get("Content-Length")
                    if declared:
                        try:
                            declared_size = int(declared)
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > cap:
                            raise AttachmentDownloadError(
                                f"attachment too large ({declared_size} bytes > {cap} byte cap)"
                            )
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(READ_CHUNK_BYTES):
                    buf.extend(chunk)
                    if cap and len(buf) > cap:
                        raise AttachmentDownloadError(
                            f"attachment too large (> {cap} byte cap)"
                        )
                return bytes(buf)
    except AttachmentDownloadError:
        raise
    except asyncio.TimeoutError as exc:
        raise AttachmentDownloadError(
            f"attachment download timed out after {timeout:.0f}s"
        ) from exc
    except aiohttp.ClientError as exc:
        raise AttachmentDownloadError(
            f"attachment download error: {type(exc).__name__}: {exc}"
        ) from exc
    except OSError as exc:
        raise AttachmentDownloadError(f"attachment download error: {exc}") from exc


def _cache_bytes(data: bytes, mime: str, filename: str) -> str:
    """Write ``data`` through the class-specific cache helper; return the path."""
    primary = _normalize_mime(mime)
    if primary.startswith("image/"):
        ext = ext_for_mime(primary, fallback=".jpg") or ".jpg"
        return cache_image_from_bytes(data, ext)
    if primary.startswith("audio/"):
        ext = ext_for_mime(primary, fallback=".ogg") or ".ogg"
        return cache_audio_from_bytes(data, ext)
    if primary.startswith("video/"):
        ext = ext_for_mime(primary, fallback=".mp4") or ".mp4"
        return cache_video_from_bytes(data, ext)
    name = filename or f"attachment{ext_for_mime(primary, fallback='')}"
    return cache_document_from_bytes(data, name)


async def cache_inbound_attachments(
    attachments: List[dict], *, flags: int = 0
) -> Tuple[List[str], List[str], Optional[MessageType]]:
    """Download + cache every message attachment; return ``(urls, types, message_type)``.

    ``flags`` is the message-level flags int (``VOICE_MESSAGE`` 8192 marks a
    voice note; the per-attachment ``flags`` field is honored too).  Oversize
    and failed downloads are logged and skipped — a partial result is returned.
    ``message_type`` is the strongest cached media class (image > audio > video >
    document; voice note wins over plain audio) or ``None`` when nothing cached.
    """
    media_urls: List[str] = []
    media_types: List[str] = []
    best: Optional[MessageType] = None
    message_flags = _as_int(flags)

    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        filename = str(att.get("filename") or "")
        mime = _attachment_mime(att)
        voice_note = _is_voice_note(message_flags, _as_int(att.get("flags")))
        label = filename or str(att.get("id") or "?")
        cap = get_inbound_media_max_bytes()
        size = _as_int(att.get("size"))
        if cap and size and size > cap:
            logger.warning(
                "Fluxer: skipping attachment %s (%d bytes > %d byte inbound cap)",
                label, size, cap,
            )
            continue
        url = att.get("url") or att.get("proxy_url")
        if not url:
            logger.warning("Fluxer: skipping attachment %s (no url)", label)
            continue
        try:
            data = await download_attachment(str(url))
        except AttachmentDownloadError as e:
            logger.warning("Fluxer: attachment %s download failed: %s", label, e)
            continue
        except Exception as e:  # never let one attachment kill the message
            logger.warning("Fluxer: attachment %s download error: %s", label, e)
            continue
        try:
            path = _cache_bytes(data, mime, filename)
        except Exception as e:
            logger.warning("Fluxer: caching attachment %s failed: %s", label, e)
            continue
        media_urls.append(path)
        media_types.append(mime or "application/octet-stream")
        candidate = message_type_for(mime, is_voice_note=voice_note)
        if best is None or _MESSAGE_TYPE_RANK.get(candidate, 0) > _MESSAGE_TYPE_RANK.get(best, 0):
            best = candidate
    return media_urls, media_types, best
