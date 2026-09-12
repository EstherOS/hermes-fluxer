"""Unit tests for ``fluxer.gatewayws`` — local websockets mock servers, no internet.

Run::

    cd /home/agent/.hermes/hermes-agent
    ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_gatewayws.py -q
"""

from __future__ import annotations

import asyncio
import functools
import json
import time

import pytest
import websockets

from fluxer.gatewayws import FluxerGatewayClient, FluxerGatewayError

RAW_TOKEN = "1547828742208888832.raw-secret"


def asynctest(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


async def start_mock(handler):
    """Start a local websockets server; return (server, ws_url)."""
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


async def close_mock(server):
    server.close()
    await server.wait_closed()


def guarded(fn):
    """Swallow expected ConnectionClosed noise from mock handlers."""

    @functools.wraps(fn)
    async def wrapper(ws):
        try:
            await fn(ws)
        except websockets.ConnectionClosed:
            pass

    return wrapper


async def do_handshake(ws, *, interval_ms=200, session_id="S1", user_id="u-1"):
    """HELLO → read IDENTIFY → READY.  Returns the identify frame."""
    await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": interval_ms}}))
    identify = json.loads(await asyncio.wait_for(ws.recv(), 5))
    await ws.send(
        json.dumps(
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {"session_id": session_id, "user": {"id": user_id}, "version": 1},
            }
        )
    )
    return identify


async def ack_heartbeats(ws):
    """Ack heartbeat frames until the connection ends."""
    async for raw in ws:
        frame = json.loads(raw)
        if frame.get("op") == 1:
            await ws.send(json.dumps({"op": 11, "d": None}))


async def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def dummy_on_event(_t, _d):
    return None


# ---------------------------------------------------------------------------
# handshake / identify / ready
# ---------------------------------------------------------------------------


@asynctest
async def test_start_sends_identify_and_returns_after_ready():
    state = {}
    events = []

    @guarded
    async def handler(ws):
        state["identify"] = await do_handshake(ws, interval_ms=500)
        await ack_heartbeats(ws)

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN,
        url=url,
        on_event=lambda t, d: _record(events, t, d),
        retry_backoff=[0.05],
    )
    try:
        await client.start()
        assert client.user_id == "u-1"
        assert client.session_id == "S1"
        assert client.ready_payload["session_id"] == "S1"
        assert await wait_for(lambda: any(t == "READY" for t, _ in events))

        identify = state["identify"]
        assert identify["op"] == 2
        assert identify["d"]["token"] == RAW_TOKEN
        assert not identify["d"]["token"].startswith("Bot ")  # raw token on the gateway
        assert identify["d"]["properties"] == {
            "os": "Linux",
            "browser": "hermes-fluxer",
            "device": "hermes-fluxer",
        }
        assert identify["d"]["presence"] == {"status": "online", "afk": False}
        assert "intents" not in identify["d"]
    finally:
        await client.stop()
        await close_mock(server)


async def _record(events, t, d):
    events.append((t, d))


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


@asynctest
async def test_heartbeat_sent_and_acked():
    state = {"beats": 0}

    @guarded
    async def handler(ws):
        await do_handshake(ws, interval_ms=120)
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("op") == 1:
                state["beats"] += 1
                await ws.send(json.dumps({"op": 11, "d": None}))

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, retry_backoff=[0.05]
    )
    try:
        await client.start()
        ok = await wait_for(lambda: state["beats"] >= 2 and client._ack_count >= 1, timeout=3)
        assert ok, f"beats={state['beats']} acks={client._ack_count}"
        assert client._last_ack_ts is not None
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_heartbeat_beats_use_last_seq():
    seqs = []

    @guarded
    async def handler(ws):
        await do_handshake(ws, interval_ms=80)
        await ws.send(json.dumps({"op": 0, "t": "MESSAGE_CREATE", "s": 7, "d": {"id": "m1"}}))
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("op") == 1:
                seqs.append(frame["d"])
                await ws.send(json.dumps({"op": 11, "d": None}))

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, retry_backoff=[0.05]
    )
    try:
        await client.start()
        assert await wait_for(lambda: 7 in seqs, timeout=3)
    finally:
        await client.stop()
        await close_mock(server)


# ---------------------------------------------------------------------------
# dispatch routing / isolation
# ---------------------------------------------------------------------------


@asynctest
async def test_dispatch_events_routed_and_seq_tracked():
    events = []

    @guarded
    async def handler(ws):
        await do_handshake(ws)
        await ws.send(
            json.dumps(
                {"op": 0, "t": "MESSAGE_CREATE", "s": 2, "d": {"id": "m1", "content": "hi"}}
            )
        )
        await ack_heartbeats(ws)

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=lambda t, d: _record(events, t, d), retry_backoff=[0.05]
    )
    try:
        await client.start()
        assert await wait_for(lambda: any(t == "MESSAGE_CREATE" for t, _ in events))
        payload = next(d for t, d in events if t == "MESSAGE_CREATE")
        assert payload == {"id": "m1", "content": "hi"}
        assert client._seq == 2
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_handler_error_is_isolated_per_event():
    seen = []

    async def on_event(t, d):
        seen.append(t)
        if t == "MESSAGE_CREATE":
            raise RuntimeError("boom from the adapter")

    @guarded
    async def handler(ws):
        await do_handshake(ws)
        await ws.send(json.dumps({"op": 0, "t": "MESSAGE_CREATE", "s": 2, "d": {"id": "m1"}}))
        await ws.send(json.dumps({"op": 0, "t": "MESSAGE_UPDATE", "s": 3, "d": {"id": "m1"}}))
        await ack_heartbeats(ws)

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(RAW_TOKEN, url=url, on_event=on_event, retry_backoff=[0.05])
    try:
        await client.start()
        assert await wait_for(lambda: "MESSAGE_UPDATE" in seen)
        assert client.is_connected
    finally:
        await client.stop()
        await close_mock(server)


# ---------------------------------------------------------------------------
# reconnect / resume
# ---------------------------------------------------------------------------


@asynctest
async def test_resume_after_drop():
    state = {"conns": 0, "resume_frame": None}
    conn_events = []

    @guarded
    async def handler(ws):
        state["conns"] += 1
        if state["conns"] == 1:
            await do_handshake(ws)
            await asyncio.sleep(0.1)
            await ws.close(code=1000)
            return
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 300}}))
        state["resume_frame"] = json.loads(await asyncio.wait_for(ws.recv(), 5))
        await ws.send(json.dumps({"op": 0, "t": "RESUMED", "s": 3, "d": {}}))
        await ack_heartbeats(ws)

    async def on_conn(kind, payload):
        conn_events.append((kind, payload))

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, on_connection_event=on_conn,
        retry_backoff=[0.05],
    )
    try:
        await client.start()
        assert await wait_for(lambda: state["resume_frame"] is not None), "no resume attempt"
        frame = state["resume_frame"]
        assert frame["op"] == 6
        assert frame["d"]["token"] == RAW_TOKEN
        assert frame["d"]["session_id"] == "S1"
        assert frame["d"]["seq"] >= 1

        assert await wait_for(lambda: ("resumed", None) in conn_events)
        kinds = [k for k, _ in conn_events]
        assert kinds[0] == "ready"
        assert "disconnected" in kinds
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_resume_rejected_falls_back_to_fresh_identify():
    state = {"conns": 0, "frames": []}
    conn_events = []

    @guarded
    async def handler(ws):
        state["conns"] += 1
        number = state["conns"]
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 300}}))
        state["frames"].append(json.loads(await asyncio.wait_for(ws.recv(), 5)))
        if number == 1:
            await ws.send(
                json.dumps(
                    {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "S1", "user": {"id": "u-1"}}}
                )
            )
            await asyncio.sleep(0.1)
            await ws.close(code=1000)
            return
        if number == 2:
            await ws.send(json.dumps({"op": 9, "d": False}))  # resume rejected
            await ws.close(code=1000)
            return
        await ws.send(
            json.dumps(
                {"op": 0, "t": "READY", "s": 2, "d": {"session_id": "S2", "user": {"id": "u-1"}}}
            )
        )
        await ack_heartbeats(ws)

    async def on_conn(kind, payload):
        conn_events.append((kind, payload))

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, on_connection_event=on_conn,
        retry_backoff=[0.05],
    )
    try:
        await client.start()
        assert await wait_for(lambda: state["conns"] >= 3 and len(state["frames"]) >= 3)
        assert state["frames"][1]["op"] == 6  # resume was attempted
        assert state["frames"][2]["op"] == 2  # then a fresh IDENTIFY
        assert await wait_for(lambda: client.session_id == "S2")
        assert [k for k, _ in conn_events].count("ready") == 2
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_close_4004_is_non_retryable():
    state = {"conns": 0}
    conn_events = []

    @guarded
    async def handler(ws):
        state["conns"] += 1
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 300}}))
        await asyncio.wait_for(ws.recv(), 5)
        await ws.close(code=4004, reason="invalid token")

    async def on_conn(kind, payload):
        conn_events.append((kind, payload))

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, on_connection_event=on_conn,
        retry_backoff=[0.05],
    )
    try:
        with pytest.raises(FluxerGatewayError) as excinfo:
            await client.start()
        assert excinfo.value.retryable is False

        kinds = [k for k, _ in conn_events]
        assert "disconnected" in kinds and "reconnect_failed" in kinds
        disconnected = next(p for k, p in conn_events if k == "disconnected")
        assert disconnected["code"] == 4004
        failed = next(p for k, p in conn_events if k == "reconnect_failed")
        assert failed["non_retryable"] is True

        await asyncio.sleep(0.2)
        assert state["conns"] == 1  # no retry after 4004
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_heartbeat_ack_timeout_forces_reconnect():
    state = {"conns": 0}

    @guarded
    async def handler(ws):
        state["conns"] += 1
        await do_handshake(ws, interval_ms=80)
        async for _raw in ws:  # consume heartbeats, never ack
            pass

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN,
        url=url,
        on_event=dummy_on_event,
        ack_timeout=0.4,
        retry_backoff=[0.05],
    )
    try:
        await client.start()
        assert await wait_for(lambda: state["conns"] >= 2, timeout=4), "no reconnect after ack timeout"
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_backoff_ladder_is_1_2_5_10_30_60_capped():
    client = FluxerGatewayClient(RAW_TOKEN, on_event=dummy_on_event)
    try:
        assert [client._backoff_delay(i) for i in range(7)] == [1, 2, 5, 10, 30, 60, 60]
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# send helpers / shutdown
# ---------------------------------------------------------------------------


@asynctest
async def test_presence_and_voice_state_helpers():
    state = {"frames": []}

    @guarded
    async def handler(ws):
        await do_handshake(ws)
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("op") == 1:
                await ws.send(json.dumps({"op": 11, "d": None}))
            else:
                state["frames"].append(frame)

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, retry_backoff=[0.05]
    )
    try:
        await client.start()
        await client.set_presence("idle")
        await client.update_voice_state(
            "g1", "c1", self_mute=False, self_deaf=True, self_video=False, self_stream=False
        )
        assert await wait_for(lambda: len(state["frames"]) >= 2)

        presence, voice = state["frames"][0], state["frames"][1]
        assert presence["op"] == 3
        assert presence["d"] == {"status": "idle", "afk": False}
        assert voice["op"] == 4
        assert voice["d"] == {
            "guild_id": "g1",
            "channel_id": "c1",
            "self_mute": False,
            "self_deaf": True,
            "self_video": False,
            "self_stream": False,
        }
    finally:
        await client.stop()
        await close_mock(server)


@asynctest
async def test_send_raw_frame_size_guard():
    client = FluxerGatewayClient(RAW_TOKEN, on_event=dummy_on_event)
    try:
        with pytest.raises(ValueError):
            await client.send_raw(2, {"blob": "x" * 5000})  # > 4096 bytes

        with pytest.raises(FluxerGatewayError) as excinfo:
            await client.send_raw(1, None)  # small frame, but no socket yet
        assert excinfo.value.retryable is True
    finally:
        await client.stop()


@asynctest
async def test_stop_is_clean_and_no_reconnect_afterwards():
    state = {"conns": 0}

    @guarded
    async def handler(ws):
        state["conns"] += 1
        await do_handshake(ws)
        await ack_heartbeats(ws)

    server, url = await start_mock(handler)
    client = FluxerGatewayClient(
        RAW_TOKEN, url=url, on_event=dummy_on_event, retry_backoff=[0.05]
    )
    try:
        await client.stop()  # idempotent before start
        await client.start()
        assert client.is_connected
        await client.stop()
        assert client._ws is None
        assert client._loop_task is None
        await asyncio.sleep(0.2)
        assert state["conns"] == 1
        await client.stop()  # idempotent after stop
    finally:
        await client.stop()
        await close_mock(server)
