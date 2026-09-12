"""Unit tests for the fluxer adapter (spec §2.4) — no network, fake clients only.

Run::

    cd /home/agent/.hermes/hermes-agent
    ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_adapter.py -q

Covers: the MESSAGE_CREATE filter chain (each gate + trigger policy), mention strip,
outbound chunking (≤4000, newline-aware), send error mapping (retryable on 5xx /
429-exhausted), typing throttle, env_enablement, platform-lock ordering, register().
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import fluxer.adapter as adapter_mod
from fluxer.adapter import (
    MAX_MESSAGE_LENGTH,
    TYPING_THROTTLE_SECONDS,
    FluxerAdapter,
    _env_enablement,
    _standalone_send,
    check_requirements,
    register,
    validate_config,
)
from fluxer.rest import FluxerAPIError

BOT_ID = "1547828742208888832"
OTHER_ID = "1473728643747861346"
CHANNEL = "1547815091221561347"
GUILD = "1547815091221561344"
TOKEN = f"{BOT_ID}.tokensecret"

logger_name = "fluxer.adapter"


# ── helpers ──────────────────────────────────────────────────────────────────

def make_config(**extra):
    return SimpleNamespace(extra=dict(extra))


def make_adapter(**extra) -> FluxerAdapter:
    adapter = FluxerAdapter(config=make_config(**extra))
    adapter._bot_id = BOT_ID
    adapter._write_runtime_status_safe = lambda *a, **k: None  # never touch a real HERMES_HOME
    return adapter


def payload(*, author_id=OTHER_ID, content="hello", bot=False, channel_id=CHANNEL,
            channel_type=0, guild_id=GUILD, message_id="m1", attachments=None,
            username="kairo") -> dict:
    data = {
        "id": message_id, "channel_id": channel_id, "content": content,
        "channel_type": channel_type, "guild_id": guild_id,
        "timestamp": "2026-09-11T08:00:00.000Z",
        "author": {"id": author_id, "username": username, "bot": bot},
    }
    if attachments is not None:
        data["attachments"] = attachments
    return data


class FakeREST:
    """Records calls; per-call results injected through ``create_results``."""

    def __init__(self):
        self.calls = []
        self.create_results = []  # results or exceptions, popped per create_message call
        self.me = {"id": BOT_ID, "username": "Esther"}
        self.channels = {}
        self.typing_calls = 0
        self.closed = False
        self.deleted = []

    async def get_me(self):
        self.calls.append(("get_me",))
        return self.me

    async def get_channel(self, channel_id):
        self.calls.append(("get_channel", channel_id))
        return self.channels.get(channel_id, {"id": channel_id, "name": None, "type": 0})

    async def create_message(self, channel_id, *, content=None, attachments=None,
                             message_reference=None, **kwargs):
        self.calls.append(("create_message", channel_id, content, message_reference, attachments))
        if self.create_results:
            result = self.create_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        count = sum(1 for c in self.calls if c[0] == "create_message")
        return {"id": f"msg-{count}"}

    async def edit_message(self, channel_id, message_id, *, content=None, **kwargs):
        self.calls.append(("edit_message", channel_id, message_id, content))
        return {"id": message_id, "content": content}

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete_message", channel_id, message_id))
        self.deleted.append(message_id)

    async def send_typing(self, channel_id):
        self.typing_calls += 1

    async def close(self):
        self.closed = True


class FakeRESTWithUpload(FakeREST):
    async def upload_attachment(self, channel_id, file_path, *, content_type=None, filename=None):
        self.calls.append(("upload_attachment", channel_id, file_path))
        return {"id": 0, "filename": "f.png", "upload_filename": "f.png",
                "file_size": 3, "content_type": "image/png"}


def run_event(adapter, data):
    """Feed one MESSAGE_CREATE payload through the adapter; return captured events."""
    captured = []

    async def fake_handle(event):
        captured.append(event)

    adapter.handle_message = fake_handle
    adapter._message_handler = fake_handle
    asyncio.run(adapter._handle_message_create(data))
    return captured


def create_calls(rest) -> list:
    return [c for c in rest.calls if c[0] == "create_message"]


@pytest.fixture
def fake_env(monkeypatch):
    """Replace the scoped-secret reader with a plain dict (default: token absent)."""
    values: dict = {}
    monkeypatch.setattr(adapter_mod, "_get_scoped_secret",
                        lambda name, default=None: values.get(name, default))
    return values


# ── filter chain ─────────────────────────────────────────────────────────────

def test_filter_self_message_dropped(fake_env):
    assert run_event(make_adapter(), payload(author_id=BOT_ID, content="my own reply")) == []


def test_filter_bot_message_dropped_by_default(fake_env):
    assert run_event(make_adapter(), payload(bot=True)) == []


def test_filter_bot_message_allowed_when_ignore_bots_false(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter(ignore_bots=False)
    adapter._require_mention = False
    events = run_event(adapter, payload(bot=True, content="webhook-ish"))
    assert len(events) == 1


def test_filter_unauthorized_users_fail_closed(fake_env):
    adapter = make_adapter(free_response_channels=[CHANNEL])
    assert run_event(adapter, payload()) == []
    # ...and the rejection is logged once per user, not once per message
    assert run_event(adapter, payload(content="second")) == []
    assert OTHER_ID in adapter._unauthorized_logged


def test_filter_allowlist_accepts_listed_user(fake_env):
    fake_env["FLUXER_ALLOWED_USERS"] = f"{OTHER_ID}, {BOT_ID}"
    adapter = make_adapter(free_response_channels=[CHANNEL])
    assert len(run_event(adapter, payload())) == 1


def test_filter_allow_all_env_accepts_everyone(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter(free_response_channels=[CHANNEL])
    assert len(run_event(adapter, payload())) == 1


def test_filter_guild_requires_trigger(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "1"
    adapter = make_adapter()  # not free-response, no mention, no command prefix
    assert run_event(adapter, payload(content="just chatting")) == []


def test_filter_free_response_channel_accepts(fake_env, caplog):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter(free_response_channels=[CHANNEL])
    with caplog.at_level(logging.INFO, logger=logger_name):
        events = run_event(adapter, payload(content="hi there"))
    assert len(events) == 1
    assert "trigger=free_response" in caplog.text
    assert events[0].text == "hi there"


def test_filter_mention_trigger_strips_mention(fake_env, caplog):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter()
    with caplog.at_level(logging.INFO, logger=logger_name):
        events = run_event(adapter, payload(content=f"yo <@{BOT_ID}> check this <@!{BOT_ID}>"))
    assert len(events) == 1
    assert events[0].text == "yo check this"
    assert "trigger=mention" in caplog.text


def test_filter_command_prefix_trigger(fake_env, caplog):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter()
    with caplog.at_level(logging.INFO, logger=logger_name):
        events = run_event(adapter, payload(content="/new"))
    assert len(events) == 1
    assert events[0].text == "/new"
    assert "trigger=command" in caplog.text


def test_filter_mention_pattern_trigger(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter(mention_patterns=[r"(?i)\bhey\s+esther\b"])
    events = run_event(adapter, payload(content="Hey Esther, status?"))
    assert len(events) == 1


def test_filter_dm_always_accepts(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter()
    events = run_event(adapter, payload(channel_type=1, guild_id=None, content="no mention here"))
    assert len(events) == 1
    assert events[0].source.chat_type == "dm"


def test_filter_group_dm_is_dm(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter()
    events = run_event(adapter, payload(channel_type=3, guild_id=None))
    assert len(events) == 1
    assert events[0].source.chat_type == "dm"


def test_filter_require_mention_false_accepts_unmentioned(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter(require_mention=False)
    assert len(run_event(adapter, payload(content="plain guild message"))) == 1


def test_filter_mention_only_message_dropped(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter()
    assert run_event(adapter, payload(content=f"<@{BOT_ID}>")) == []


def test_filter_source_fields_for_guild_message(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    adapter = make_adapter(free_response_channels=[CHANNEL])
    adapter._rest = FakeREST()
    adapter._rest.channels[CHANNEL] = {"id": CHANNEL, "name": "general", "type": 0}
    events = run_event(adapter, payload(message_id="m42"))
    source = events[0].source
    assert source.chat_id == CHANNEL
    assert source.chat_name == "general"       # resolved lazily from REST
    assert source.chat_type == "group"         # discord convention for guild text channels
    assert source.user_id == OTHER_ID
    assert source.scope_id == GUILD
    assert source.message_id == "m42"
    assert events[0].message_id == "m42"


def test_mention_strip_keeps_other_mentions(fake_env):
    adapter = make_adapter()
    text = adapter._strip_bot_mention(f"hi <@{BOT_ID}> and <@{OTHER_ID}> there")
    assert text == f"hi and <@{OTHER_ID}> there"


# ── outbound sending ─────────────────────────────────────────────────────────

def test_send_success(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    result = asyncio.run(adapter.send(CHANNEL, "hello"))
    assert result.success is True
    assert result.message_id == "msg-1"
    assert len(create_calls(adapter._rest)) == 1
    assert create_calls(adapter._rest)[0][2] == "hello"


def test_send_not_connected(fake_env):
    result = asyncio.run(make_adapter().send(CHANNEL, "hello"))
    assert result.success is False and result.retryable is True
    assert result.error_kind == "transient"


def test_send_refuses_empty(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    result = asyncio.run(adapter.send(CHANNEL, "   "))
    assert result.success is False
    assert create_calls(adapter._rest) == []


def test_send_chunks_long_messages(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    text = ("word " * 2000).strip()  # ~10k chars
    result = asyncio.run(adapter.send(CHANNEL, text))
    calls = create_calls(adapter._rest)
    assert result.success is True
    assert len(calls) >= 3
    for call in calls:
        assert len(call[2]) <= MAX_MESSAGE_LENGTH
    words = set(text.split())
    for call in calls:
        body = call[2]
        # strip the multi-chunk "(i/n)" indicator before word checks
        body = body.rsplit(" (", 1)[0] if body.endswith(")") and " (" in body[-10:] else body
        assert body  # never an empty chunk
        for token in body.split():
            assert token in words  # never split mid-word
    assert result.message_id == f"msg-{len(calls)}"
    assert len(result.continuation_message_ids) == len(calls)


def test_send_chunks_on_newlines(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    p1, p2 = "a" * 3000, "b" * 3000
    result = asyncio.run(adapter.send(CHANNEL, f"{p1}\n{p2}"))
    calls = create_calls(adapter._rest)
    assert result.success is True
    assert len(calls) == 2
    # split at the newline, paragraph kept whole (multi-chunk indicator appended by
    # the shared truncate_message — discord convention)
    assert calls[0][2].startswith(p1) and calls[0][2][len(p1):].strip() == "(1/2)"
    assert calls[1][2].startswith(p2) and calls[1][2][len(p2):].strip() == "(2/2)"
    assert all(len(c[2]) <= MAX_MESSAGE_LENGTH for c in calls)


def test_send_reply_reference_only_on_first_chunk(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    text = "x " * 3000
    asyncio.run(adapter.send(CHANNEL, text, reply_to="m0",
                             metadata={"channel_id": CHANNEL, "guild_id": GUILD}))
    calls = create_calls(adapter._rest)
    assert len(calls) > 1
    assert calls[0][3] == {"message_id": "m0", "channel_id": CHANNEL, "guild_id": GUILD}
    assert all(call[3] is None for call in calls[1:])


def test_send_reply_reference_not_guessed(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    asyncio.run(adapter.send(CHANNEL, "hi", reply_to="m0"))
    assert create_calls(adapter._rest)[0][3] == {"message_id": "m0"}  # no invented channel_id


def test_send_5xx_is_retryable_transient(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    adapter._rest.create_results = [FluxerAPIError(503, "upstream unavailable")]
    result = asyncio.run(adapter.send(CHANNEL, "hello"))
    assert result.success is False and result.retryable is True
    assert result.error_kind == "transient"
    assert "503" in result.error


def test_send_429_exhausted_is_retryable_rate_limited(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    adapter._rest.create_results = [
        FluxerAPIError(429, "slow down", code="RATE_LIMITED", retry_after=1.5)]
    result = asyncio.run(adapter.send(CHANNEL, "hello"))
    assert result.success is False and result.retryable is True
    assert result.error_kind == "rate_limited"
    assert result.retry_after == 1.5


def test_send_403_not_retryable(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    adapter._rest.create_results = [FluxerAPIError(403, "Missing Permissions (403 forbidden)")]
    result = asyncio.run(adapter.send(CHANNEL, "hello"))
    assert result.success is False and result.retryable is False
    assert result.error_kind == "forbidden"


def test_send_network_error_is_retryable(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    adapter._rest.create_results = [ConnectionResetError("connection reset by peer")]
    result = asyncio.run(adapter.send(CHANNEL, "hello"))
    assert result.success is False and result.retryable is True
    assert result.error_kind == "transient"


def test_send_partial_chunks_report_continuation_ids(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    text = "word " * 2000
    adapter._rest.create_results = [{"id": "first"}, FluxerAPIError(500, "boom")]
    result = asyncio.run(adapter.send(CHANNEL, text))
    assert result.success is False
    assert result.message_id == "first"          # last delivered chunk
    assert result.continuation_message_ids == ("first",)
    assert result.retryable is True


# ── typing / passthroughs / chat info ────────────────────────────────────────

def test_send_typing_throttles_per_chat(fake_env, monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock["now"])
    adapter = make_adapter()
    adapter._rest = FakeREST()
    asyncio.run(adapter.send_typing(CHANNEL))          # sends
    asyncio.run(adapter.send_typing(CHANNEL))          # throttled
    clock["now"] += 1.0
    asyncio.run(adapter.send_typing(CHANNEL))          # still throttled
    clock["now"] += TYPING_THROTTLE_SECONDS
    asyncio.run(adapter.send_typing(CHANNEL))          # sends again
    asyncio.run(adapter.send_typing("other-chat"))     # per-chat key → sends
    assert adapter._rest.typing_calls == 3


def test_send_typing_noop_when_not_connected(fake_env):
    asyncio.run(make_adapter().send_typing(CHANNEL))  # must not raise


def test_edit_and_delete_passthroughs(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    edited = asyncio.run(adapter.edit_message(CHANNEL, "m1", "new text"))
    assert edited.success is True and edited.message_id == "m1"
    assert asyncio.run(adapter.delete_message(CHANNEL, "m1")) is True
    assert adapter._rest.deleted == ["m1"]


def test_delete_message_failure_returns_false(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    adapter._rest.delete_message = _raiser(FluxerAPIError(404, "Unknown Message"))
    assert asyncio.run(adapter.delete_message(CHANNEL, "gone")) is False


def _raiser(exc):
    async def _raise(*args, **kwargs):
        raise exc
    return _raise


def test_get_chat_info_maps_types(fake_env):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    adapter._rest.channels = {
        CHANNEL: {"id": CHANNEL, "name": "general", "type": 0},
        "dm1": {"id": "dm1", "type": 1},
    }
    info = asyncio.run(adapter.get_chat_info(CHANNEL))
    assert info == {"name": "general", "type": "channel", "chat_id": CHANNEL}
    dm = asyncio.run(adapter.get_chat_info("dm1"))
    assert dm["type"] == "dm"


# ── connect / disconnect / connection events ─────────────────────────────────

def test_connect_orders_lock_after_identity(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    events = []

    class FakeRESTFactory(FakeREST):
        def __init__(self, token, base_url=None):
            super().__init__()
            events.append(("rest_init", token, base_url))

        async def get_me(self):
            events.append(("get_me",))
            return self.me

    class FakeGatewayClient:
        def __init__(self, token, *, url=None, on_event=None, on_connection_event=None):
            events.append(("ws_init", token, url))
            self.user_id = None

        async def start(self):
            events.append(("ws_start",))
            self.user_id = BOT_ID

        async def stop(self):
            events.append(("ws_stop",))

    monkeypatch.setattr(adapter_mod, "FluxerREST", FakeRESTFactory)
    monkeypatch.setattr(adapter_mod, "FluxerGatewayClient", FakeGatewayClient)
    adapter = make_adapter()
    adapter._acquire_platform_lock = (
        lambda scope, identity, desc: events.append(("lock", scope, identity, desc)) or True)
    adapter._wire_plugin_handlers = lambda native=None: events.append(("wire", native))
    adapter._mark_connected = lambda: events.append(("mark_connected",))
    assert asyncio.run(adapter.connect()) is True
    order = [e[0] for e in events]
    assert order == ["rest_init", "get_me", "lock", "ws_init", "ws_start", "wire", "mark_connected"]
    lock = next(e for e in events if e[0] == "lock")
    assert lock == ("lock", "fluxer", BOT_ID, f"Fluxer bot {BOT_ID}")


def test_connect_missing_token_fatal_non_retryable(fake_env):
    adapter = make_adapter()
    assert asyncio.run(adapter.connect()) is False
    assert adapter._fatal_error_code == "token_missing"
    assert adapter._fatal_error_retryable is False


def test_connect_failure_releases_lock_and_is_fatal(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    events = []

    class FakeGatewayClient:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            raise RuntimeError("boom")

        async def stop(self):
            events.append("ws_stop")

    monkeypatch.setattr(adapter_mod, "FluxerREST", lambda token, base_url=None: FakeREST())
    monkeypatch.setattr(adapter_mod, "FluxerGatewayClient", FakeGatewayClient)
    adapter = make_adapter()
    adapter._acquire_platform_lock = lambda *a, **k: True
    adapter._release_platform_lock = lambda: events.append("lock_released")
    assert asyncio.run(adapter.connect()) is False
    assert adapter._fatal_error_code == "connect_failed"
    assert "lock_released" in events


def test_disconnect_stops_transport_and_releases_lock(fake_env):
    adapter = make_adapter()
    events = []
    rest, ws = FakeREST(), SimpleNamespace(stop=lambda: None)

    async def ws_stop():
        events.append("ws_stop")

    ws.stop = ws_stop
    adapter._rest, adapter._ws = rest, ws
    adapter._release_platform_lock = lambda: events.append("lock_released")
    asyncio.run(adapter.disconnect())
    assert events == ["ws_stop", "lock_released"]
    assert rest.closed is True
    assert adapter._rest is None and adapter._ws is None


def test_connection_events_update_state(fake_env):
    adapter = make_adapter()
    adapter._connected_once = True
    marks = []
    adapter._mark_degraded = lambda: marks.append("degraded")
    adapter._mark_connected = lambda: marks.append("connected")
    fatals, notifies = [], []

    def fake_fatal(code, message, *, retryable):
        fatals.append((code, retryable))

    async def fake_notify():
        notifies.append(True)

    adapter._set_fatal_error = fake_fatal
    adapter._notify_fatal_error = fake_notify

    asyncio.run(adapter._on_connection_event("disconnected", {"code": 1006, "reason": "drop"}))
    assert marks == ["degraded"]
    asyncio.run(adapter._on_connection_event("ready", {"user": {"id": BOT_ID}}))
    asyncio.run(adapter._on_connection_event("resumed", None))
    assert marks == ["degraded", "connected", "connected"]
    asyncio.run(adapter._on_connection_event("reconnect_failed", {"reason": "gave up"}))
    assert fatals == [("gateway_reconnect_failed", True)]
    assert notifies == [True]


def test_connection_events_quiet_during_initial_connect(fake_env):
    adapter = make_adapter()  # _connected_once False
    marks = []
    adapter._mark_degraded = lambda: marks.append("degraded")
    adapter._set_fatal_error = lambda *a, **k: marks.append("fatal")
    asyncio.run(adapter._on_connection_event("disconnected", {"code": 1006}))
    asyncio.run(adapter._on_connection_event("reconnect_failed", {"reason": "x"}))
    assert marks == []


# ── registration helpers ─────────────────────────────────────────────────────

def test_env_enablement(fake_env):
    assert _env_enablement() is None                      # no token → not auto-enableable
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    assert _env_enablement() == {}
    fake_env["FLUXER_HOME_CHANNEL"] = CHANNEL
    fake_env["FLUXER_HOME_CHANNEL_NAME"] = "general"
    seed = _env_enablement()
    assert seed["home_channel"] == {"chat_id": CHANNEL, "name": "general"}
    fake_env["FLUXER_API_BASE"] = "http://127.0.0.1:1/v1"
    fake_env["FLUXER_GATEWAY_URL"] = "ws://127.0.0.1:1"
    seed = _env_enablement()
    assert seed["api_base"] == "http://127.0.0.1:1/v1"
    assert seed["gateway_url"] == "ws://127.0.0.1:1"


def test_validate_config_and_check_requirements(fake_env):
    assert validate_config(None) is False
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    assert validate_config(None) is True
    assert check_requirements() is True                    # aiohttp+websockets import in the venv
    fake_env.pop("FLUXER_BOT_TOKEN")
    assert check_requirements() is False


def test_register_smoke(fake_env):
    captured = {}

    class Ctx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

    register(Ctx())
    assert captured["name"] == "fluxer"
    assert captured["label"] == "Fluxer"
    assert captured["adapter_factory"] is FluxerAdapter
    assert captured["check_fn"] is check_requirements
    assert captured["ensure_deps_fn"] is None
    assert captured["validate_config"] is validate_config
    assert captured["is_connected"] is not None
    assert captured["required_env"] == ["FLUXER_BOT_TOKEN"]
    assert captured["setup_fn"] is not None and callable(captured["setup_fn"])
    assert captured["env_enablement_fn"] is not None
    assert captured["cron_deliver_env_var"] == "FLUXER_HOME_CHANNEL"
    assert captured["standalone_sender_fn"] is not None and callable(captured["standalone_sender_fn"])
    assert captured["allowed_users_env"] == "FLUXER_ALLOWED_USERS"
    assert captured["allow_all_env"] == "FLUXER_ALLOW_ALL_USERS"
    assert captured["max_message_length"] == 4000
    assert captured["emoji"] == "⚡"
    assert captured["pii_safe"] is False
    assert captured["allow_update_command"] is True
    assert "Fluxer" in captured["platform_hint"]


# ── standalone sender (cron path) ────────────────────────────────────────────

def test_standalone_send_success(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    instances = []

    def factory(token, base_url=None):
        rest = FakeREST()
        instances.append(rest)
        return rest

    monkeypatch.setattr(adapter_mod, "FluxerREST", factory)
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "hello from cron"))
    assert result == {"success": True, "message_id": "msg-1"}
    assert instances[0].closed is True


def test_standalone_send_chunks(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    rest = FakeREST()
    monkeypatch.setattr(adapter_mod, "FluxerREST", lambda token, base_url=None: rest)
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "word " * 2000))
    assert result["success"] is True
    calls = create_calls(rest)
    assert len(calls) >= 3
    assert all(len(c[2]) <= MAX_MESSAGE_LENGTH for c in calls)
    assert result["message_id"] == f"msg-{len(calls)}"


def test_standalone_send_missing_token(fake_env):
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "hello"))
    assert "error" in result and "FLUXER_BOT_TOKEN" in result["error"]


def test_standalone_send_empty_message(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    monkeypatch.setattr(adapter_mod, "FluxerREST", lambda token, base_url=None: FakeREST())
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "   "))
    assert "error" in result


def test_standalone_send_error_mapping(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    rest = FakeREST()
    rest.create_results = [FluxerAPIError(404, "Unknown Channel")]
    monkeypatch.setattr(adapter_mod, "FluxerREST", lambda token, base_url=None: rest)
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "hello"))
    assert "error" in result and "404" in result["error"]


def test_standalone_send_media_requires_upload_support(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    monkeypatch.setattr(adapter_mod, "FluxerREST", lambda token, base_url=None: FakeREST())
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "with file",
                                          media_files=["/tmp/x.png"]))
    assert "error" in result and "upload_attachment" in result["error"]


def test_standalone_send_media_uploads_and_claims(fake_env, monkeypatch):
    fake_env["FLUXER_BOT_TOKEN"] = TOKEN
    rest = FakeRESTWithUpload()
    monkeypatch.setattr(adapter_mod, "FluxerREST", lambda token, base_url=None: rest)
    result = asyncio.run(_standalone_send(make_config(), CHANNEL, "with file",
                                          media_files=["/tmp/x.png"]))
    assert result["success"] is True
    uploads = [c for c in rest.calls if c[0] == "upload_attachment"]
    assert uploads == [("upload_attachment", CHANNEL, "/tmp/x.png")]
    assert create_calls(rest)[0][4] == [
        {"id": 0, "filename": "f.png", "upload_filename": "f.png",
         "file_size": 3, "content_type": "image/png"}]
