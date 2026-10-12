"""Fluxer REST client (spec ``§2.1`` — frozen interface).

Async :mod:`aiohttp` client for ``https://api.fluxer.app/v1``:

* auth header ``Authorization: Bot <token>`` (REST only — the gateway uses the
  raw token);
* rate-limit aware: parses ``X-RateLimit-*`` into a small bucket map, honours
  429 ``Retry-After``/``retry_after`` with a capped sleep and at most
  :data:`MAX_RETRIES` retries; retries 5xx/network/timeout with backoff; never
  retries other 4xx;
* attachments use the presigned upload flow exactly per ``docs/fluxer-api-notes.md``
  §3 — plan → PUT bytes to the returned ``upload_url`` (verbatim, **no** auth
  header; the query string is the credential) → ``/attachments/complete`` for
  multipart.

Layout details that are not frozen by the spec (constants, helper names) are
implementation details; the public surface is ``FluxerAPIError`` and
``FluxerREST``.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp

__all__ = ["FluxerAPIError", "FluxerREST", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "https://api.fluxer.app/v1"

#: Attachments up to this size use the singlepart flow; larger use multipart.
SINGLEPART_LIMIT = 10 * 1024 * 1024  # 10 MiB = 10_485_760 bytes
#: Bots are clamped to 50 MiB regardless of the resolved user limit.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

#: Retry budget for one logical request: at most 3 retries (429s and
#: transient failures share the budget).
MAX_RETRIES = 3
#: Hard cap on any single rate-limit sleep so a pathological ``Retry-After``
#: cannot stall the bot for minutes.
RETRY_DELAY_CAP = 30.0
#: Backoff ladder for 5xx / transport retries.
SERVER_ERROR_BACKOFF = (1.0, 2.0, 4.0)
#: Statuses accepted when the caller does not narrow ``expected``.
DEFAULT_EXPECTED = (200, 201, 204)
#: Per-request timeout.
DEFAULT_TIMEOUT = 30.0

#: Long numeric ids in paths collapse to ``{id}`` for bucket bookkeeping.
_ID_RE = re.compile(r"\d{8,}")


class FluxerAPIError(Exception):
    """Fluxer API error (``{code, message, errors}`` envelope) or transport failure.

    ``status`` is the HTTP status (``0`` for a transport-level failure that
    never produced a response); ``retry_after`` is set for 429s when the
    server provided it.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"HTTP {status} [{code or 'error'}]: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _route_key(method: str, path: str) -> str:
    """Bucket key granularity: method + path with ids collapsed."""
    return f"{method.upper()} {_ID_RE.sub('{id}', path.split('?', 1)[0])}"


def _decode(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _code_of(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        code = payload.get("code")
        return str(code) if code is not None else None
    return None


def _message_of(payload: Any, default: str) -> str:
    if isinstance(payload, Mapping):
        message = payload.get("message") or default
        errors = payload.get("errors")
        if errors:
            message = f"{message} ({errors})"
        return str(message)[:500]
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:500]
    return default


def _retry_after(payload: Any, header_value: str | None) -> float:
    """429 retry delay in seconds: body ``retry_after`` first, then header."""
    candidates: list[Any] = []
    if isinstance(payload, Mapping) and payload.get("retry_after") is not None:
        candidates.append(payload["retry_after"])
    if header_value is not None:
        candidates.append(header_value)
    for value in candidates:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return 1.0


def _backoff(retries_done: int) -> float:
    """Backoff for the n-th retry (1-based), clamped to the ladder."""
    index = min(max(retries_done, 1), len(SERVER_ERROR_BACKOFF)) - 1
    return SERVER_ERROR_BACKOFF[index]


class FluxerREST:
    """Async REST client for the Fluxer bot API."""

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL) -> None:
        if not token:
            raise ValueError("FluxerREST requires a bot token")
        self._token = token
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        self._route_buckets: dict[str, str] = {}
        self._buckets: dict[str, dict[str, Any]] = {}

    # -- introspection -----------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def rate_limits(self) -> dict[str, dict[str, Any]]:
        """Snapshot of the best-effort rate-limit bucket map."""
        return {key: dict(value) for key, value in self._buckets.items()}

    # -- lifecycle ---------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session (idempotent)."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    # -- rate-limit bookkeeping -------------------------------------------

    def _record_rate_limit(self, route_key: str, headers: Mapping[str, str]) -> None:
        bucket = headers.get("X-RateLimit-Bucket")
        if not bucket:
            return
        self._route_buckets[route_key] = bucket
        info = self._buckets.setdefault(bucket, {})
        for header, key, as_int in (
            ("X-RateLimit-Limit", "limit", True),
            ("X-RateLimit-Remaining", "remaining", True),
            ("X-RateLimit-Reset-After", "reset_after", False),
            ("X-RateLimit-Reset", "reset", False),
        ):
            raw = headers.get(header)
            if raw is None:
                continue
            try:
                info[key] = int(float(raw)) if as_int else float(raw)
            except (TypeError, ValueError):
                continue
        info["updated_at"] = time.monotonic()

    async def _pre_sleep(self, route_key: str) -> None:
        """Best-effort proactive sleep when a known bucket is exhausted."""
        bucket = self._route_buckets.get(route_key)
        if not bucket:
            return
        info = self._buckets.get(bucket)
        if not info or info.get("remaining", 1) > 0:
            return
        reset_after = float(info.get("reset_after") or 0.0)
        if reset_after > 0:
            await self._sleep(min(reset_after, RETRY_DELAY_CAP))

    async def _sleep(self, seconds: float) -> None:
        """Single sleep hook (patchable in tests)."""
        await asyncio.sleep(max(0.0, seconds))

    # -- core request ------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        files: Any = None,
        expected: Sequence[int] = DEFAULT_EXPECTED,
    ) -> Any:
        """Perform a rate-limit aware API request and return parsed JSON.

        Raises :class:`FluxerAPIError` for error envelopes, unexpected
        statuses and transport failures.  ``expected`` lists the accepted
        success statuses (default 200/201/204).
        """
        method = method.upper()
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = self._base_url + (path if path.startswith("/") else f"/{path}")
        route_key = _route_key(method, path)
        retries = 0

        while True:
            await self._pre_sleep(route_key)
            headers = {
                "Authorization": f"Bot {self._token}",
                "Accept": "application/json",
            }
            try:
                session = await self._get_session()
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=files,
                ) as resp:
                    status = resp.status
                    self._record_rate_limit(route_key, resp.headers)
                    retry_after_header = resp.headers.get("Retry-After")
                    payload = _decode(await resp.text())
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if retries < MAX_RETRIES:
                    retries += 1
                    await self._sleep(_backoff(retries))
                    continue
                raise FluxerAPIError(
                    0,
                    f"{type(exc).__name__}: {exc}",
                    code="TRANSPORT_ERROR",
                ) from exc

            if status == 429:
                retry_after = _retry_after(payload, retry_after_header)
                if retries < MAX_RETRIES:
                    retries += 1
                    await self._sleep(min(retry_after, RETRY_DELAY_CAP))
                    continue
                raise FluxerAPIError(
                    429,
                    _message_of(payload, "rate limited"),
                    code=_code_of(payload) or "RATE_LIMITED",
                    retry_after=retry_after,
                )

            if 500 <= status < 600:
                if retries < MAX_RETRIES:
                    retries += 1
                    await self._sleep(_backoff(retries))
                    continue
                raise FluxerAPIError(
                    status,
                    _message_of(payload, f"server error {status}"),
                    code=_code_of(payload),
                )

            if status in expected:
                return payload

            raise FluxerAPIError(
                status,
                _message_of(payload, f"unexpected status {status}"),
                code=_code_of(payload),
            )

    # -- convenience methods ----------------------------------------------

    async def get_me(self) -> dict:
        return await self.request("GET", "/users/@me")

    async def get_gateway_info(self) -> dict:
        return await self.request("GET", "/gateway/bot")

    async def get_channel(self, channel_id: str) -> dict:
        return await self.request("GET", f"/channels/{channel_id}")

    async def get_guild(self, guild_id: str) -> dict:
        return await self.request("GET", f"/guilds/{guild_id}")

    async def list_guild_channels(self, guild_id: str) -> list[dict]:
        return await self.request("GET", f"/guilds/{guild_id}/channels")

    async def list_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return await self.request("GET", f"/channels/{channel_id}/messages", params=params)

    async def create_message(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embeds: list | None = None,
        attachments: list | None = None,
        message_reference: dict | None = None,
        allowed_mentions: dict | None = None,
        nonce: Any = None,
        flags: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embeds is not None:
            body["embeds"] = embeds
        if attachments is not None:
            body["attachments"] = attachments
        if message_reference is not None:
            body["message_reference"] = message_reference
        if allowed_mentions is not None:
            body["allowed_mentions"] = allowed_mentions
        if nonce is not None:
            body["nonce"] = nonce
        if flags is not None:
            # Sendable set (api notes §2): VOICE_MESSAGE 8192 etc.; others are dropped server-side.
            body["flags"] = flags
        return await self.request("POST", f"/channels/{channel_id}/messages", json_body=body)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        content: str | None = None,
        embeds: list | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embeds is not None:
            body["embeds"] = embeds
        return await self.request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}", json_body=body
        )

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self.request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    async def send_typing(self, channel_id: str) -> None:
        await self.request("POST", f"/channels/{channel_id}/typing")

    async def open_dm(self, recipient_id: str) -> dict:
        return await self.request(
            "POST", "/users/@me/channels", json_body={"recipient_id": recipient_id}
        )

    # -- attachments -------------------------------------------------------

    async def _put_bytes(self, url: str, data: bytes, content_type: str | None = None) -> int:
        """PUT bytes to a presigned ``upload_url`` — **no** auth header.

        The URL is used verbatim (its query string is the authorization; it
        must never be rebuilt).  ``content_type`` is sent for singlepart
        uploads only.
        """
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise FluxerAPIError(0, f"invalid upload_url: {url!r}", code="INVALID_UPLOAD_URL")
        headers = {"Content-Type": content_type} if content_type else {}
        retries = 0
        while True:
            try:
                session = await self._get_session()
                async with session.put(url, data=data, headers=headers) as resp:
                    status = resp.status
                    await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                if retries < MAX_RETRIES:
                    retries += 1
                    await self._sleep(_backoff(retries))
                    continue
                raise FluxerAPIError(
                    0, f"upload PUT transport error: {exc}", code="TRANSPORT_ERROR"
                ) from exc
            if 200 <= status < 300:
                return status
            if (500 <= status < 600) and retries < MAX_RETRIES:
                retries += 1
                await self._sleep(_backoff(retries))
                continue
            raise FluxerAPIError(
                status,
                f"upload PUT failed for {url.split('?', 1)[0]}",
                code="UPLOAD_FAILED",
            )

    async def upload_attachment(
        self,
        channel_id: str,
        file_path: str,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> dict:
        """Upload one file and return the claim dict for ``create_message``.

        Flow (api-notes §3): plan → PUT (singlepart) or PUT parts + complete
        (multipart, >10 MiB) → ``{"id", "filename", "upload_filename",
        "file_size", "content_type"}``.
        """
        path = Path(file_path)
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment too large: {size} bytes > {MAX_ATTACHMENT_BYTES} (bot limit)"
            )
        filename = filename or path.name
        content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        data = path.read_bytes()

        plan = await self.request(
            "POST",
            f"/channels/{channel_id}/attachments",
            json_body={
                "attachments": [
                    {
                        "id": 0,
                        "filename": filename,
                        "file_size": len(data),
                        "content_type": content_type,
                    }
                ]
            },
        )
        items = plan.get("attachments") if isinstance(plan, Mapping) else plan
        if not isinstance(items, list) or not items:
            raise FluxerAPIError(200, "attachment plan response missing items", code="INVALID_PLAN")
        item = items[0]

        mode = str(item.get("upload_mode") or "singlepart")
        if mode == "singlepart":
            await self._put_bytes(item["upload_url"], data, content_type)
            upload_filename = item["upload_filename"]
        elif mode == "multipart":
            part_size = int(item["part_size"])
            parts = sorted(item.get("parts") or [], key=lambda p: int(p["part_number"]))
            for part in parts:
                number = int(part["part_number"])
                chunk = data[(number - 1) * part_size : number * part_size]
                await self._put_bytes(part["upload_url"], chunk)
            complete = await self.request(
                "POST",
                f"/channels/{channel_id}/attachments/complete",
                json_body={
                    "uploads": [
                        {
                            "upload_filename": item["upload_filename"],
                            "upload_id": item["upload_id"],
                        }
                    ]
                },
            )
            upload_filename = item["upload_filename"]
            uploads = complete.get("uploads") if isinstance(complete, Mapping) else None
            if isinstance(uploads, list) and uploads and uploads[0].get("upload_filename"):
                upload_filename = uploads[0]["upload_filename"]
        else:
            raise FluxerAPIError(200, f"unknown upload_mode {mode!r}", code="INVALID_PLAN")

        return {
            "id": item.get("id", 0),
            "filename": filename,
            "upload_filename": upload_filename,
            "file_size": len(data),
            "content_type": content_type,
        }
