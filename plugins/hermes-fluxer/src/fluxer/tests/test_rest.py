"""Unit tests for ``fluxer.rest`` — no network, local aiohttp test servers.

Run::

    cd /home/agent/.hermes/hermes-agent
    ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_rest.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import socket
import tempfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from fluxer.rest import (
    MAX_ATTACHMENT_BYTES,
    RETRY_DELAY_CAP,
    FluxerAPIError,
    FluxerREST,
)


def asynctest(fn):
    """Run an async test body without depending on pytest-asyncio."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@contextlib.asynccontextmanager
async def fluxer_server(routes):
    """Start a local aiohttp server; yield its ``/v1`` base URL.

    Route paths are declared without the ``/v1`` prefix; it is added here so
    the fixtures mirror the real API paths.
    """
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, "/v1" + path, handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}/v1"
    finally:
        await server.close()


def no_sleep(rest, sleeps):
    """Patch the client's sleep hook so retries do not slow the suite down."""

    async def fake(seconds):
        sleeps.append(seconds)

    rest._sleep = fake  # instance attribute shadows the method


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# auth / envelope / status handling
# ---------------------------------------------------------------------------


@asynctest
async def test_auth_header_and_get_me():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["accept"] = request.headers.get("Accept")
        return web.json_response({"id": "1547828742208888832", "username": "Esther", "bot": True})

    async with fluxer_server([("GET", "/users/@me", handler)]) as base:
        rest = FluxerREST("123456.secret", base_url=base)
        try:
            me = await rest.get_me()
        finally:
            await rest.close()

    assert seen["auth"] == "Bot 123456.secret"
    assert seen["accept"] == "application/json"
    assert me["username"] == "Esther"


@asynctest
async def test_error_envelope_maps_to_api_error_without_retry():
    calls = []

    async def handler(request):
        calls.append(1)
        return web.json_response(
            {
                "code": "MISSING_AUTHORIZATION",
                "message": "authentication required",
                "errors": [{"path": "authorization", "code": "MISSING", "message": "missing"}],
            },
            status=401,
        )

    async with fluxer_server([("GET", "/users/@me", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            with pytest.raises(FluxerAPIError) as excinfo:
                await rest.get_me()
        finally:
            await rest.close()

    err = excinfo.value
    assert err.status == 401
    assert err.code == "MISSING_AUTHORIZATION"
    assert "authentication required" in err.message
    assert err.retry_after is None
    assert len(calls) == 1  # 4xx other than 429 is never retried


@asynctest
async def test_unexpected_success_status_raises():
    async def handler(request):
        return web.json_response({"ok": True}, status=202)

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            with pytest.raises(FluxerAPIError) as excinfo:
                await rest.request("GET", "/thing", expected=(200,))
        finally:
            await rest.close()

    assert excinfo.value.status == 202


# ---------------------------------------------------------------------------
# 429 handling
# ---------------------------------------------------------------------------


@asynctest
async def test_retry_on_429_honors_retry_after():
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return web.json_response(
                {"code": "RATE_LIMITED", "message": "slow down", "retry_after": 0.25},
                status=429,
                headers={"Retry-After": "0.25", "X-RateLimit-Scope": "user"},
            )
        return web.json_response({"ok": True})

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        sleeps = []
        no_sleep(rest, sleeps)
        try:
            result = await rest.request("GET", "/thing")
        finally:
            await rest.close()

    assert result == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [0.25]  # body retry_after honoured


@asynctest
async def test_429_retries_are_capped_at_three():
    calls = []

    async def handler(request):
        calls.append(1)
        return web.json_response(
            {"code": "RATE_LIMITED", "message": "still limited", "retry_after": 0.05},
            status=429,
        )

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        sleeps = []
        no_sleep(rest, sleeps)
        try:
            with pytest.raises(FluxerAPIError) as excinfo:
                await rest.request("GET", "/thing")
        finally:
            await rest.close()

    assert excinfo.value.status == 429
    assert excinfo.value.code == "RATE_LIMITED"
    assert excinfo.value.retry_after == pytest.approx(0.05)
    assert len(calls) == 4  # 1 attempt + max 3 retries
    assert len(sleeps) == 3


@asynctest
async def test_429_retry_after_is_capped():
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return web.json_response(
                {"code": "RATE_LIMITED", "message": "wild number", "retry_after": 9999},
                status=429,
            )
        return web.json_response({"ok": True})

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        sleeps = []
        no_sleep(rest, sleeps)
        try:
            await rest.request("GET", "/thing")
        finally:
            await rest.close()

    assert sleeps == [RETRY_DELAY_CAP]


# ---------------------------------------------------------------------------
# 5xx / transport handling
# ---------------------------------------------------------------------------


@asynctest
async def test_5xx_retried_with_backoff_then_succeeds():
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return web.json_response({"code": "INTERNAL", "message": "boom"}, status=500)
        return web.json_response({"ok": True})

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        sleeps = []
        no_sleep(rest, sleeps)
        try:
            result = await rest.request("GET", "/thing")
        finally:
            await rest.close()

    assert result == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # backoff ladder


@asynctest
async def test_5xx_exhausted_raises():
    calls = []

    async def handler(request):
        calls.append(1)
        return web.json_response({"code": "INTERNAL", "message": "boom"}, status=503)

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        sleeps = []
        no_sleep(rest, sleeps)
        try:
            with pytest.raises(FluxerAPIError) as excinfo:
                await rest.request("GET", "/thing")
        finally:
            await rest.close()

    assert excinfo.value.status == 503
    assert len(calls) == 4


@asynctest
async def test_transport_error_retried_then_raised():
    rest = FluxerREST("tok", base_url=f"http://127.0.0.1:{free_port()}/v1")
    sleeps = []
    no_sleep(rest, sleeps)
    try:
        with pytest.raises(FluxerAPIError) as excinfo:
            await rest.request("GET", "/thing")
    finally:
        await rest.close()

    assert excinfo.value.status == 0
    assert excinfo.value.code == "TRANSPORT_ERROR"
    assert len(sleeps) == 3


# ---------------------------------------------------------------------------
# rate-limit bookkeeping
# ---------------------------------------------------------------------------


@asynctest
async def test_rate_limit_headers_recorded_in_bucket_map():
    async def handler(request):
        return web.json_response(
            {"ok": True},
            headers={
                "X-RateLimit-Bucket": "668bcb75babe0f52",
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "59",
                "X-RateLimit-Reset-After": "0.167",
                "X-RateLimit-Reset": "1758000000.5",
            },
        )

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            await rest.request("GET", "/thing")
            buckets = rest.rate_limits
        finally:
            await rest.close()

    info = buckets["668bcb75babe0f52"]
    assert info["limit"] == 60
    assert info["remaining"] == 59
    assert info["reset_after"] == pytest.approx(0.167)
    assert info["reset"] == pytest.approx(1758000000.5)


@asynctest
async def test_exhausted_bucket_triggers_pre_sleep():
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return web.json_response(
                {"ok": 1},
                headers={
                    "X-RateLimit-Bucket": "bkt",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset-After": "0.2",
                },
            )
        return web.json_response({"ok": 2})

    async with fluxer_server([("GET", "/thing", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        sleeps = []
        no_sleep(rest, sleeps)
        try:
            await rest.request("GET", "/thing")
            await rest.request("GET", "/thing")
        finally:
            await rest.close()

    assert len(calls) == 2
    assert sleeps == [pytest.approx(0.2)]  # pre-sleep before the second call


# ---------------------------------------------------------------------------
# convenience methods
# ---------------------------------------------------------------------------


@asynctest
async def test_create_message_body_shaping_and_201():
    seen = {}

    async def handler(request):
        seen["json"] = await request.json()
        seen["method"] = request.method
        return web.json_response({"id": "m1"}, status=201)

    async with fluxer_server([("POST", "/channels/c1/messages", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            message = await rest.create_message(
                "c1",
                content="hello",
                message_reference={"message_id": "m0", "channel_id": "c1"},
                allowed_mentions={"parse": [], "replied_user": False},
            )
        finally:
            await rest.close()

    assert message == {"id": "m1"}
    assert seen["method"] == "POST"
    assert seen["json"] == {
        "content": "hello",
        "message_reference": {"message_id": "m0", "channel_id": "c1"},
        "allowed_mentions": {"parse": [], "replied_user": False},
    }
    assert "embeds" not in seen["json"] and "attachments" not in seen["json"]


@asynctest
async def test_list_messages_query_params():
    seen = {}

    async def handler(request):
        seen["query"] = dict(request.query)
        return web.json_response([{"id": "m1"}])

    async with fluxer_server([("GET", "/channels/c1/messages", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            messages = await rest.list_messages("c1", limit=25, before="b1")
        finally:
            await rest.close()

    assert messages == [{"id": "m1"}]
    assert seen["query"] == {"limit": "25", "before": "b1"}


@asynctest
async def test_delete_message_and_send_typing():
    seen = []

    async def delete_handler(request):
        seen.append(("DELETE", request.path))
        return web.Response(status=204)

    async def typing_handler(request):
        seen.append(("POST", request.path))
        return web.Response(status=204)

    async with fluxer_server(
        [
            ("DELETE", "/channels/c1/messages/m1", delete_handler),
            ("POST", "/channels/c1/typing", typing_handler),
        ]
    ) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            deleted = await rest.delete_message("c1", "m1")
            typed = await rest.send_typing("c1")
        finally:
            await rest.close()

    assert deleted is None
    assert typed is None
    assert seen == [
        ("DELETE", "/v1/channels/c1/messages/m1"),
        ("POST", "/v1/channels/c1/typing"),
    ]


@asynctest
async def test_open_dm_posts_recipient():
    seen = {}

    async def handler(request):
        seen["json"] = await request.json()
        return web.json_response({"id": "dm1", "type": 1})

    async with fluxer_server([("POST", "/users/@me/channels", handler)]) as base:
        rest = FluxerREST("tok", base_url=base)
        try:
            channel = await rest.open_dm("1473728643747861346")
        finally:
            await rest.close()

    assert channel == {"id": "dm1", "type": 1}
    assert seen["json"] == {"recipient_id": "1473728643747861346"}


# ---------------------------------------------------------------------------
# attachments (presigned upload flow)
# ---------------------------------------------------------------------------


@asynctest
async def test_upload_attachment_singlepart():
    state = {"puts": [], "plan_body": None}

    async def plan(request):
        state["plan_body"] = await request.json()
        upload_url = f"{request.scheme}://{request.host}/v1/relay/key1?t=cap-secret&x=1"
        return web.json_response(
            {
                "attachments": [
                    {
                        "id": 0,
                        "filename": "pixel.png",
                        "upload_filename": "claim-path/uf1",
                        "file_size": 4,
                        "content_type": "image/png",
                        "upload_mode": "singlepart",
                        "upload_url": upload_url,
                    }
                ]
            }
        )

    async def put(request):
        state["puts"].append(
            {
                "path_qs": request.path_qs,
                "auth": request.headers.get("Authorization"),
                "ctype": request.headers.get("Content-Type"),
                "body": await request.read(),
            }
        )
        return web.Response(status=200)

    with tempfile.TemporaryDirectory() as tmp:
        file_path = Path(tmp) / "pixel.png"
        file_path.write_bytes(b"\x89PNG")

        async with fluxer_server(
            [
                ("POST", "/channels/c1/attachments", plan),
                ("PUT", "/relay/key1", put),
            ]
        ) as base:
            rest = FluxerREST("tok", base_url=base)
            try:
                claim = await rest.upload_attachment("c1", str(file_path))
            finally:
                await rest.close()

    assert state["plan_body"] == {
        "attachments": [
            {"id": 0, "filename": "pixel.png", "file_size": 4, "content_type": "image/png"}
        ]
    }
    assert claim == {
        "id": 0,
        "filename": "pixel.png",
        "upload_filename": "claim-path/uf1",
        "file_size": 4,
        "content_type": "image/png",
    }
    assert len(state["puts"]) == 1
    put = state["puts"][0]
    assert put["path_qs"] == "/v1/relay/key1?t=cap-secret&x=1"  # upload_url used verbatim
    assert put["auth"] is None  # query string is the auth — never send the bot header
    assert put["ctype"] == "image/png"
    assert put["body"] == b"\x89PNG"


@asynctest
async def test_upload_attachment_multipart():
    state = {"puts": [], "complete_body": None}

    async def plan(request):
        host = f"{request.scheme}://{request.host}"
        parts = [
            {"part_number": 3, "upload_url": f"{host}/v1/relay/p3?t=c3"},
            {"part_number": 1, "upload_url": f"{host}/v1/relay/p1?t=c1"},
            {"part_number": 2, "upload_url": f"{host}/v1/relay/p2?t=c2"},
        ]
        return web.json_response(
            {
                "attachments": [
                    {
                        "id": 0,
                        "filename": "big.bin",
                        "upload_filename": "claim-path/uf2",
                        "file_size": 10,
                        "content_type": "application/octet-stream",
                        "upload_mode": "multipart",
                        "upload_id": "up1",
                        "part_size": 4,
                        "parts": parts,
                    }
                ]
            }
        )

    def make_put(name):
        async def put(request):
            state["puts"].append((name, await request.read()))
            return web.Response(status=200)

        return put

    async def complete(request):
        state["complete_body"] = await request.json()
        return web.json_response(
            {"uploads": [{"upload_filename": "claim-path/uf2-final", "upload_id": "up1"}]}
        )

    with tempfile.TemporaryDirectory() as tmp:
        file_path = Path(tmp) / "big.bin"
        file_path.write_bytes(b"AAAABBBBCC")  # 10 bytes → 4 + 4 + 2

        async with fluxer_server(
            [
                ("POST", "/channels/c1/attachments", plan),
                ("PUT", "/relay/p1", make_put("p1")),
                ("PUT", "/relay/p2", make_put("p2")),
                ("PUT", "/relay/p3", make_put("p3")),
                ("POST", "/channels/c1/attachments/complete", complete),
            ]
        ) as base:
            rest = FluxerREST("tok", base_url=base)
            try:
                claim = await rest.upload_attachment("c1", str(file_path))
            finally:
                await rest.close()

    assert sorted(state["puts"]) == [
        ("p1", b"AAAA"),
        ("p2", b"BBBB"),
        ("p3", b"CC"),
    ]
    assert state["complete_body"] == {
        "uploads": [{"upload_filename": "claim-path/uf2", "upload_id": "up1"}]
    }
    assert claim["upload_filename"] == "claim-path/uf2-final"
    assert claim["file_size"] == 10


@asynctest
async def test_upload_attachment_oversize_rejected_locally():
    with tempfile.TemporaryDirectory() as tmp:
        file_path = Path(tmp) / "huge.bin"
        with open(file_path, "wb") as handle:
            handle.truncate(MAX_ATTACHMENT_BYTES + 1)  # sparse, cheap

        rest = FluxerREST("tok", base_url=f"http://127.0.0.1:{free_port()}/v1")
        try:
            with pytest.raises(ValueError):
                await rest.upload_attachment("c1", str(file_path))
        finally:
            await rest.close()
