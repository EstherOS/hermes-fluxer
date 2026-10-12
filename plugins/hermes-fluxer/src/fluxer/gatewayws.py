"""Fluxer gateway WebSocket client (spec ``§2.2`` — frozen interface).

Implements the Fluxer gateway protocol against ``wss://gateway.fluxer.app``
(URL override supported; ``?v=1&encoding=json`` appended automatically):

* HELLO → heartbeat loop (server-provided interval, jittered first beat, ack
  tracking, reconnect when no ack within :data:`ACK_TIMEOUT`);
* IDENTIFY with the **raw** token (no ``Bot `` prefix — REST differs!);
* dispatch ``{op:0, t, s, d}`` routed to ``on_event(t, d)`` with per-event
  error isolation; ``seq`` tracked for resume;
* reconnect with RESUME (``session_id`` + ``seq``, 60 s window) on a fresh
  socket, falling back to a fresh IDENTIFY when the server rejects it;
  backoff ladder 1, 2, 5, 10, 30, 60 s;
* connection events via ``on_connection_event``:
  ``("ready", d)`` / ``("resumed", None)`` / ``("disconnected", {"code", "reason"})``
  / ``("reconnect_failed", {...})`` — close code 4004 (and op9 identify
  rejection) marks the failure ``non_retryable: True``.

Additive (optional keyword) knobs beyond the frozen signature — safe for the
frozen callers, used by tests and to bound behaviour:
``ack_timeout``, ``retry_backoff``, ``max_reconnect_attempts``,
``start_timeout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

import websockets

__all__ = ["FluxerGatewayClient", "FluxerGatewayError"]

log = logging.getLogger(__name__)

DEFAULT_GATEWAY_URL = "wss://gateway.fluxer.app"
#: Query appended to the socket URL (no compression; JSON encoding).
GATEWAY_QUERY = "?v=1&encoding=json"
#: Fallback heartbeat interval if HELLO omits one (live value: 41250 ms).
DEFAULT_HEARTBEAT_INTERVAL_MS = 41250
#: No heartbeat ack within this many seconds → recycle the socket.
ACK_TIMEOUT = 45.0
#: Session resume window after a drop.
RESUME_WINDOW = 60.0
#: Reconnect backoff ladder (seconds), capped at the last value.
BACKOFF_SEQUENCE = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
#: Client → server frames must stay under 4096 bytes (close 4002 otherwise).
MAX_SEND_BYTES = 4096
#: Inbound frame cap (the server may send much larger frames).
MAX_FRAME_BYTES = 2**24
#: Close codes that will never succeed on retry (bad token / bad API version).
NON_RETRYABLE_CLOSE_CODES = frozenset({4003, 4004, 4012})


class FluxerGatewayError(RuntimeError):
    """Gateway failure surfaced to the caller (``start``/``send`` paths)."""

    def __init__(self, message: str, *, retryable: bool = True, code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class _Fatal(Exception):
    """Internal: stop the reconnect loop (non-retryable condition)."""

    def __init__(self, reason: str, *, code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class _ProtocolError(Exception):
    """Internal: malformed frame / unexpected handshake state."""


def _close_code(exc: BaseException) -> int | None:
    """Best-effort close-code extraction from a websockets exception.

    Prefers the received/sent close frames (``ConnectionClosed.code`` is
    deprecated in websockets ≥ 13) with a fallback for plain exceptions.
    """
    for obj in (getattr(exc, "rcvd", None), getattr(exc, "sent", None), exc):
        try:
            code = getattr(obj, "code", None)
        except Exception:  # pragma: no cover - defensive
            continue
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


class FluxerGatewayClient:
    """Async gateway client.  Usage::

        client = FluxerGatewayClient(token, on_event=handler)
        await client.start()          # returns after READY
        ...
        await client.stop()
    """

    RESUME_WINDOW = RESUME_WINDOW
    ACK_TIMEOUT = ACK_TIMEOUT

    def __init__(
        self,
        token: str,
        *,
        url: str = DEFAULT_GATEWAY_URL,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_connection_event: Callable[[str, dict | None], Awaitable[None]] | None = None,
        ack_timeout: float = ACK_TIMEOUT,
        retry_backoff: Sequence[float] | None = None,
        max_reconnect_attempts: int = 10,
        start_timeout: float = 30.0,
    ) -> None:
        if not token:
            raise ValueError("FluxerGatewayClient requires a bot token")
        if not callable(on_event):
            raise ValueError("FluxerGatewayClient requires an async on_event callback")
        self._token = token  # raw token: IDENTIFY must NOT carry the "Bot " prefix
        self._url = (url or DEFAULT_GATEWAY_URL).rstrip("/")
        self._on_event = on_event
        self._on_connection_event = on_connection_event
        self.ack_timeout = float(ack_timeout)
        self.backoff = tuple(retry_backoff) if retry_backoff else BACKOFF_SEQUENCE
        self.max_reconnect_attempts = int(max_reconnect_attempts)
        self.start_timeout = float(start_timeout)

        self._ws: Any = None
        self._hb_task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._ready_event = asyncio.Event()
        self._closed = False
        self._failure: dict[str, Any] | None = None

        # session state
        self._session_id: str | None = None
        self._seq: int | None = None
        self._user_id: str | None = None
        self._ready_payload: dict | None = None
        self._last_drop_ts: float | None = None

        # heartbeat / connection bookkeeping
        self._conn_started_ts: float | None = None
        self._last_beat_ts: float | None = None
        self._last_ack_ts: float | None = None
        self._ack_count = 0
        self._handshake_ok = False
        self._identify_pending = False
        self._resume_pending = False

    # -- public state ------------------------------------------------------

    @property
    def user_id(self) -> str | None:
        """Bot user id, from the READY payload."""
        return self._user_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def ready_payload(self) -> dict | None:
        return self._ready_payload

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._closed

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Connect, HELLO → IDENTIFY → wait for READY, then keep running.

        Returns once READY has been received; heartbeat and dispatch loops
        continue in the background.  Raises :class:`FluxerGatewayError`
        (``retryable`` attribute set) when the connection cannot be
        established.
        """
        if self._loop_task is not None and not self._loop_task.done():
            raise RuntimeError("FluxerGatewayClient.start() called while already running")
        self._closed = False
        self._failure = None
        self._ready_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._connection_loop(), name="fluxer-gateway")
        try:
            await asyncio.wait_for(self._wait_ready_or_fail(), self.start_timeout)
        except asyncio.TimeoutError:
            await self.stop()
            raise FluxerGatewayError(
                "timed out waiting for gateway READY", retryable=True
            ) from None

    async def _wait_ready_or_fail(self) -> None:
        while True:
            if self._ready_event.is_set():
                return
            if self._loop_task is not None and self._loop_task.done():
                if not self._ready_event.is_set():
                    failure = self._failure
                    if failure:
                        raise FluxerGatewayError(
                            failure["reason"],
                            retryable=failure.get("retryable", True),
                            code=failure.get("code"),
                        )
                    exc = self._loop_task.exception()
                    raise FluxerGatewayError(
                        str(exc) if exc else "gateway loop ended before READY",
                        retryable=True,
                    )
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        """Graceful shutdown: cancel loops and close the socket."""
        self._closed = True
        hb, loop, ws = self._hb_task, self._loop_task, self._ws
        self._hb_task = None
        self._loop_task = None
        self._ws = None
        current = asyncio.current_task()
        for task in (hb, loop):
            if task is not None and task is not current and not task.done():
                task.cancel()
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="client shutdown")
        for task in (hb, loop):
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # -- socket plumbing ---------------------------------------------------

    def _build_url(self) -> str:
        if "encoding=" in self._url:
            return self._url
        separator = "&" if "?" in self._url else "?"
        return f"{self._url}{separator}v=1&encoding=json"

    def _identify_data(self) -> dict:
        return {
            "token": self._token,  # raw token, no "Bot " prefix
            "properties": {
                "os": "Linux",
                "browser": "hermes-fluxer",
                "device": "hermes-fluxer",
            },
            "presence": {"status": "online", "afk": False},
        }

    def _can_resume(self) -> bool:
        if not self._session_id or self._seq is None:
            return False
        if self._last_drop_ts is None:
            return True
        return (time.monotonic() - self._last_drop_ts) <= self.RESUME_WINDOW

    def _backoff_delay(self, attempt: int) -> float:
        ladder = self.backoff or BACKOFF_SEQUENCE
        return float(ladder[min(max(attempt, 0), len(ladder) - 1)])

    async def _recv_json(self) -> Any:
        ws = self._ws
        if ws is None:
            raise _ProtocolError("gateway socket is not connected")
        raw = await ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _ProtocolError(f"invalid JSON frame: {exc}") from exc

    async def _send(self, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        size = len(data.encode("utf-8"))
        if size > MAX_SEND_BYTES:
            raise ValueError(
                f"gateway client frame is {size} bytes (limit {MAX_SEND_BYTES})"
            )
        ws = self._ws
        if ws is None:
            raise FluxerGatewayError("gateway socket is not connected", retryable=True)
        await ws.send(data)

    async def send_raw(self, op: int, d: Any = None) -> None:
        """Send a raw gateway frame (client frames must stay < 4096 bytes)."""
        await self._send({"op": int(op), "d": d})

    # -- send helpers ------------------------------------------------------

    async def _send_heartbeat(self) -> None:
        self._last_beat_ts = time.monotonic()
        await self._send({"op": 1, "d": self._seq if self._seq else None})

    async def update_voice_state(
        self,
        guild_id: str | None,
        channel_id: str | None,
        *,
        self_mute: bool = True,
        self_deaf: bool = True,
        self_video: bool = False,
        self_stream: bool = False,
    ) -> None:
        """Gateway op 4 (Voice State Update) — join/leave/move a voice channel."""
        await self.send_raw(
            4,
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "self_mute": bool(self_mute),
                "self_deaf": bool(self_deaf),
                "self_video": bool(self_video),
                "self_stream": bool(self_stream),
            },
        )

    async def set_presence(self, status: str = "online") -> None:
        """Gateway op 3 (Presence Update)."""
        await self.send_raw(3, {"status": status, "afk": False})

    # -- connection loop ---------------------------------------------------

    async def _connection_loop(self) -> None:
        attempt = 0
        while not self._closed:
            try:
                reason = await self._connect_once()
            except asyncio.CancelledError:
                raise
            except _Fatal as exc:
                await self._emit_conn(
                    "reconnect_failed",
                    {"reason": exc.reason, "code": exc.code, "non_retryable": True},
                )
                self._failure = {
                    "reason": exc.reason,
                    "retryable": False,
                    "code": exc.code,
                }
                return
            except Exception as exc:
                code = _close_code(exc)
                if code in NON_RETRYABLE_CLOSE_CODES:
                    reason_text = f"gateway closed with non-retryable code {code}"
                    await self._emit_conn(
                        "reconnect_failed",
                        {"reason": reason_text, "code": code, "non_retryable": True},
                    )
                    self._failure = {
                        "reason": reason_text,
                        "retryable": False,
                        "code": code,
                    }
                    return
                log.warning("fluxer gateway connection dropped: %s", exc)
            else:
                if reason == "invalid_session":
                    log.info("fluxer gateway: session invalid — will re-identify")

            if self._closed:
                return
            if self._handshake_ok:  # a live session was established: reset backoff
                attempt = 0
            if attempt >= self.max_reconnect_attempts:
                reason_text = "reconnect attempts exhausted"
                await self._emit_conn(
                    "reconnect_failed",
                    {"reason": reason_text, "attempts": attempt, "non_retryable": False},
                )
                self._failure = {
                    "reason": reason_text,
                    "retryable": True,
                    "code": None,
                }
                return
            delay = self._backoff_delay(attempt)
            attempt += 1
            log.warning("fluxer gateway: reconnecting in %.1fs (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)

    async def _connect_once(self) -> str | None:
        """One socket lifetime: connect → HELLO → IDENTIFY/RESUME → read loop.

        Returns a reason string when the loop should start a new socket
        (``"reconnect_requested"`` / ``"invalid_session"``); raises on socket
        failure (after emitting ``disconnected``).
        """
        url = self._build_url()
        self._handshake_ok = False
        ws = await websockets.connect(url, max_size=MAX_FRAME_BYTES, open_timeout=15)
        self._ws = ws
        self._conn_started_ts = time.monotonic()
        self._last_beat_ts = None
        self._last_ack_ts = None
        try:
            hello = await asyncio.wait_for(self._recv_json(), 15.0)
            if not isinstance(hello, Mapping) or hello.get("op") != 10:
                raise _ProtocolError(f"expected HELLO op 10, got {str(hello)[:120]!r}")
            interval_ms = int(
                ((hello.get("d") or {}) if isinstance(hello.get("d"), Mapping) else {}).get(
                    "heartbeat_interval"
                )
                or DEFAULT_HEARTBEAT_INTERVAL_MS
            )
            self._hb_task = asyncio.create_task(
                self._heartbeat_loop(interval_ms / 1000.0), name="fluxer-heartbeat"
            )

            if self._can_resume():
                log.info("fluxer gateway: resuming session %s (seq=%s)", self._session_id, self._seq)
                self._resume_pending = True
                await self._send(
                    {
                        "op": 6,
                        "d": {
                            "token": self._token,
                            "session_id": self._session_id,
                            "seq": self._seq,
                        },
                    }
                )
            else:
                self._session_id = None
                self._seq = None
                self._identify_pending = True
                await self._send({"op": 2, "d": self._identify_data()})

            while True:
                payload = await self._recv_json()
                if not isinstance(payload, Mapping):
                    continue
                op = payload.get("op")
                if op == 0:
                    await self._handle_dispatch(payload)
                elif op == 11:
                    self._last_ack_ts = time.monotonic()
                    self._ack_count += 1
                elif op == 1:  # server-requested heartbeat
                    await self._send_heartbeat()
                elif op == 7:
                    await self._emit_conn(
                        "disconnected",
                        {"code": None, "reason": "reconnect requested by gateway"},
                    )
                    return "reconnect_requested"
                elif op == 9:
                    return await self._handle_invalid_session(payload)
                elif op == 10:
                    continue  # duplicate HELLO; ignore
                else:
                    log.debug("fluxer gateway: ignoring op %s", op)
        except asyncio.CancelledError:
            raise
        except _Fatal:
            raise
        except Exception as exc:
            await self._emit_conn(
                "disconnected",
                {"code": _close_code(exc), "reason": f"{type(exc).__name__}: {exc}"},
            )
            raise
        finally:
            self._last_drop_ts = time.monotonic()
            self._ws = None
            if self._hb_task is not None:
                self._hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._hb_task
                self._hb_task = None
            with contextlib.suppress(Exception):
                await ws.close()

    async def _heartbeat_loop(self, interval: float) -> None:
        """Beat every ``interval`` seconds; recycles the socket when acks stop."""
        try:
            await asyncio.sleep(random.uniform(0.0, 1.0) * interval)  # jitter first beat
            while True:
                await self._send_heartbeat()
                await asyncio.sleep(interval)
                if self._ack_overdue():
                    log.warning(
                        "fluxer gateway: no heartbeat ack within %.0fs — reconnecting",
                        self.ack_timeout,
                    )
                    ws = self._ws
                    if ws is not None:
                        with contextlib.suppress(Exception):
                            await ws.close(code=4000)  # force recv() to raise → reconnect
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("fluxer gateway heartbeat loop exiting: %s", exc)

    def _ack_overdue(self) -> bool:
        if self._last_beat_ts is None:
            return False
        reference = self._last_ack_ts or self._conn_started_ts
        return reference is not None and (time.monotonic() - reference) > self.ack_timeout

    async def _handle_dispatch(self, payload: Mapping[str, Any]) -> None:
        seq = payload.get("s")
        if isinstance(seq, int):
            self._seq = seq
        event = payload.get("t")
        if not isinstance(event, str):
            return
        data = payload.get("d")
        if data is None:
            data = {}
        if event == "READY":
            if isinstance(data, Mapping):
                session = data.get("session_id")
                if session:
                    self._session_id = str(session)
                user = data.get("user")
                if isinstance(user, Mapping) and user.get("id"):
                    self._user_id = str(user["id"])
                self._ready_payload = dict(data)
            self._handshake_ok = True
            self._identify_pending = False
            self._resume_pending = False
            await self._emit_conn("ready", data)
            self._ready_event.set()
        elif event == "RESUMED":
            self._handshake_ok = True
            self._resume_pending = False
            await self._emit_conn("resumed", None)
        await self._dispatch_event(event, data)

    async def _handle_invalid_session(self, payload: Mapping[str, Any]) -> str:
        resumable = bool(payload.get("d"))
        if self._identify_pending and not self._handshake_ok:
            reason = "identify rejected by gateway (op9 d=false)"
            await self._emit_conn("disconnected", {"code": None, "reason": reason})
            raise _Fatal(reason, code=None)
        if not resumable:
            self._session_id = None
            self._seq = None
        self._identify_pending = False
        self._resume_pending = False
        reason = f"gateway invalid session (resumable={resumable})"
        await self._emit_conn("disconnected", {"code": None, "reason": reason})
        return "invalid_session"

    async def _dispatch_event(self, event: str, data: Any) -> None:
        try:
            await self._on_event(event, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fluxer gateway: on_event handler failed for %s (isolated)", event)

    async def _emit_conn(self, kind: str, payload: Any) -> None:
        if self._on_connection_event is None:
            return
        try:
            await self._on_connection_event(kind, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "fluxer gateway: on_connection_event handler failed for %s (isolated)", kind
            )
