#!/usr/bin/env python
"""Sandbox E2E driver for the fluxer plugin's text scope (wave 1, C2 evidence).

Phases (run from the repo root; see status/c2-report.md for the full sequence):

    nice -n 10 <venv>/python scripts/e2e_text.py send
        Create a channel webhook in #general and execute one ``[e2e]`` message that
        mentions the bot — authored OUTSIDE the bot identity. Records ids in
        ``sandbox/e2e-state.json``.

    .../e2e_text.py check [--timeout 120]
        Poll ``GET /channels/{id}/messages`` for the agent's reply (a message by
        the bot id newer than the trigger). Records the reply id.

    .../e2e_text.py cleanup
    .../e2e_text.py wait-webhook-log [--timeout 60]      # tail sandbox/gw.log for route/ignore lines
        Delete every tracked test message + the webhook, then verify each is gone.

    .../e2e_text.py inject
        Offline FALLBACK: feed a real captured MESSAGE_CREATE payload (docs/captures/)
        through a standalone FluxerAdapter with a mock handler and assert the full
        adapter pipeline yields a handle_message call with the right source/text.

Token is read from sandbox/hermes-home/.env (or FLUXER_ENV_FILE / $FLUXER_BOT_TOKEN)
and never printed. All live writes are clearly marked ``[e2e]`` and deleted by
``cleanup``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKOUT = pathlib.Path("/home/agent/.hermes/hermes-agent")
sys.path.insert(0, str(ROOT / "plugin-src"))
sys.path.insert(0, str(CHECKOUT))  # gateway.* for the inject phase

from fluxer.rest import FluxerAPIError, FluxerREST  # noqa: E402

CHANNEL = "1547815091221561347"   # #general in the test guild
GUILD = "1547815091221561344"
BOT_ID = "1547828742208888832"
STATE_PATH = ROOT / "sandbox" / "e2e-state.json"
GW_LOG = ROOT / "sandbox" / "gw.log"
TAG = "[e2e]"


def load_token() -> str:
    env_file = os.environ.get("FLUXER_ENV_FILE")
    candidates = [pathlib.Path(env_file)] if env_file else [
        ROOT / "sandbox" / "hermes-home" / ".env", ROOT / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("FLUXER_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    token = os.environ.get("FLUXER_BOT_TOKEN")
    if token:
        return token
    sys.exit(f"FLUXER_BOT_TOKEN not found (checked {', '.join(str(c) for c in candidates)})")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"messages": [], "webhook": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def note_message(state: dict, kind: str, message: dict) -> None:
    state["messages"].append({
        "kind": kind,
        "channel_id": str(message.get("channel_id") or CHANNEL),
        "id": str(message.get("id")),
        "content": (message.get("content") or "")[:80],
    })
    save_state(state)


async def with_rest(fn):
    rest = FluxerREST(load_token())
    try:
        return await fn(rest)
    finally:
        await rest.close()


# ── phases ───────────────────────────────────────────────────────────────────

async def phase_send() -> None:
    state = load_state()

    async def run(rest: FluxerREST):
        hook = state.get("webhook")
        if hook:
            try:
                await rest.request("GET", f"/webhooks/{hook['id']}")
                print(f"reusing webhook {hook['id']}")
            except FluxerAPIError as e:
                print(f"recorded webhook unusable ({e}); creating a new one")
                hook = None
        if not hook:
            created = await rest.request(
                "POST", f"/channels/{CHANNEL}/webhooks",
                json_body={"name": f"{TAG} hermes fluxer adapter"},
                expected=(200, 201))
            hook = {"id": str(created["id"]), "token": created["token"]}
            print(f"created webhook id={hook['id']} channel={created.get('channel_id')}")
        state["webhook"] = {"id": str(hook["id"]), "token": hook["token"],
                            "created_at": state.get("webhook", {}).get("created_at") or time.time()}
        save_state(state)

        content = f"{TAG} <@{BOT_ID}> ping — verify the fluxer adapter text path"
        message = await rest.request(
            "POST", f"/webhooks/{hook['id']}/{hook['token']}",
            json_body={"content": content, "username": f"{TAG} external"},
            expected=(200, 201))
        author = (message or {}).get("author") or {}
        print(f"executed webhook message id={message.get('id')} "
              f"author.id={author.get('id')} author.bot={author.get('bot')} "
              f"timestamp={message.get('timestamp')}")
        note_message(state, "trigger", message)
        state["sent_at"] = time.time()
        save_state(state)
        return message

    await with_rest(run)


async def phase_check(timeout: float) -> int:
    state = load_state()
    sent_at = float(state.get("sent_at") or 0)
    deadline = time.time() + timeout
    reply = None

    async def run(rest: FluxerREST):
        nonlocal reply
        while time.time() < deadline:
            messages = await rest.list_messages(CHANNEL, limit=20)
            for message in messages or []:
                author = (message or {}).get("author") or {}
                if str(author.get("id")) == BOT_ID:
                    ts = message.get("timestamp")
                    try:
                        parsed = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        parsed = time.time()
                    if parsed >= sent_at - 5 and (message.get("content") or "").strip():
                        reply = message
                        return
            await asyncio.sleep(5)

    await with_rest(run)
    if not reply:
        print(f"NO REPLY within {timeout:.0f}s")
        return 2
    note_message(state, "reply", reply)
    print(f"reply id={reply['id']} author={BOT_ID} content={reply.get('content')!r}")
    return 0


async def phase_wait_webhook_log(timeout: float) -> int:
    """Tail sandbox/gw.log for the adapter's route/ignore line about the trigger."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if GW_LOG.exists():
            text = GW_LOG.read_text(errors="replace")
            for line in text.splitlines():
                if "Fluxer: message from" in line or "Fluxer: ignored message from" in line:
                    print(line.strip())
                    return 0
        time.sleep(3)
    print(f"no Fluxer route/ignore line within {timeout:.0f}s")
    return 2


async def phase_cleanup() -> None:
    state = load_state()
    remaining = []

    async def run(rest: FluxerREST):
        # 1. delete tracked messages (bot auth first; webhook-token route as fallback)
        for entry in state.get("messages", []):
            channel_id, message_id = entry["channel_id"], entry["id"]
            try:
                await rest.request("DELETE", f"/channels/{channel_id}/messages/{message_id}",
                                   expected=(204, 404))
                print(f"deleted message {message_id}")
            except FluxerAPIError as e:
                hook = state.get("webhook")
                if hook and e.status in (403, 404):
                    try:
                        await rest.request(
                            "DELETE", f"/webhooks/{hook['id']}/{hook['token']}/messages/{message_id}",
                            expected=(204, 404))
                        print(f"deleted webhook message {message_id} (token route)")
                    except FluxerAPIError as e2:
                        print(f"COULD NOT delete message {message_id}: {e2}")
                        remaining.append(entry)
                else:
                    print(f"COULD NOT delete message {message_id}: {e}")
                    remaining.append(entry)
        # 2. delete the webhook
        hook = state.get("webhook")
        if hook:
            try:
                await rest.request("DELETE", f"/webhooks/{hook['id']}", expected=(204, 404))
                print(f"deleted webhook {hook['id']}")
            except FluxerAPIError as e:
                print(f"COULD NOT delete webhook {hook['id']}: {e}")
                remaining.append({"kind": "webhook", "id": hook["id"]})
        # 3. verify: every message must now 404
        for entry in state.get("messages", []):
            try:
                await rest.request("GET", f"/channels/{entry['channel_id']}/messages/{entry['id']}",
                                   expected=(200,))
                print(f"VERIFY FAILED: message {entry['id']} still exists")
                remaining.append(entry)
            except FluxerAPIError as e:
                print(f"verified gone: message {entry['id']} -> {e.status} {e.code}")

    await with_rest(run)
    state["messages"] = []
    state["webhook"] = None
    save_state(state)
    print(f"cleanup done; remaining={len(remaining)}")
    if remaining:
        save_state({"messages": remaining, "webhook": None})


async def phase_selfmsg() -> None:
    """Live bounded probe: post a marked message AS the bot in #general so the running
    sandbox gateway receives a real MESSAGE_CREATE over its WS and logs the self-gate drop."""
    state = load_state()

    async def run(rest: FluxerREST):
        content = f"{TAG} live self-message — adapter self-filter probe (deleted by cleanup)"
        message = await rest.create_message(CHANNEL, content=content)
        note_message(state, "self", message)
        print(f"sent self-message id={message.get('id')} as bot {BOT_ID}")

    await with_rest(run)


async def phase_sweep() -> None:
    """Delete any bot-authored messages in #general carrying the test markers (covers the
    standalone-sender message whose id we did not record)."""
    markers = (TAG, "standalone sender test")
    state = load_state()
    deleted = []

    async def run(rest: FluxerREST):
        messages = await rest.list_messages(CHANNEL, limit=50)
        for message in messages or []:
            author = (message or {}).get("author") or {}
            content = message.get("content") or ""
            if str(author.get("id")) == BOT_ID and any(m in content for m in markers):
                try:
                    await rest.delete_message(CHANNEL, str(message["id"]))
                    deleted.append(str(message["id"]))
                    print(f"swept message {message['id']}: {content[:60]!r}")
                except FluxerAPIError as e:
                    print(f"COULD NOT sweep {message['id']}: {e}")

    await with_rest(run)
    state["sweep_deleted"] = deleted
    save_state(state)


async def phase_inject() -> int:
    """Offline fallback: real captured MESSAGE_CREATE → standalone adapter pipeline."""
    from types import SimpleNamespace

    from gateway.config import Platform
    if "fluxer" not in Platform._value2member_map_:
        Platform._add_pseudo_member("fluxer")
    from fluxer.adapter import FluxerAdapter

    os.environ.setdefault("FLUXER_ALLOW_ALL_USERS", "true")
    capture = sorted((ROOT / "docs" / "captures").glob("*3-MESSAGE_CREATE.json"))
    payload_path = capture[0] if capture else ROOT / "docs" / "captures" / "event-20260911-075316-3-MESSAGE_CREATE.json"
    data = json.loads(payload_path.read_text())["d"]
    # rewrite to look like a fresh external message mentioning the bot
    fresh_id = str(int(time.time() * 1000)) + "00"
    data["id"] = fresh_id
    data["author"] = {"id": "9990000000000000002", "username": "e2e-external",
                      "global_name": None, "bot": False}
    data["content"] = f"{TAG} <@{BOT_ID}> ping (offline injection)"
    data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    adapter = FluxerAdapter(config=SimpleNamespace(extra={"free_response_channels": [CHANNEL]}))
    adapter._bot_id = BOT_ID
    adapter._write_runtime_status_safe = lambda *a, **k: None
    captured = []

    async def handler(event):
        captured.append(event)

    adapter._message_handler = handler
    adapter.handle_message = handler
    await adapter._handle_message_create(data)

    if not captured:
        print("INJECT FAILED: no handle_message call")
        return 2
    event = captured[0]
    source = event.source
    print(json.dumps({
        "handle_message_calls": len(captured),
        "text": event.text,
        "chat_id": source.chat_id,
        "chat_type": source.chat_type,
        "user_id": source.user_id,
        "user_name": source.user_name,
        "scope_id": source.scope_id,
        "message_id": event.message_id,
    }, indent=2))
    ok = (event.text.startswith(TAG) and BOT_ID not in event.text
          and source.chat_type == "group" and source.user_id == "9990000000000000002")
    print("INJECT", "PASS" if ok else "FAIL")
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["send", "check", "wait-webhook-log", "cleanup", "selfmsg", "sweep", "inject"])
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.phase == "send":
        asyncio.run(phase_send())
        return 0
    if args.phase == "check":
        return asyncio.run(phase_check(args.timeout))
    if args.phase == "wait-webhook-log":
        return asyncio.run(phase_wait_webhook_log(args.timeout))
    if args.phase == "cleanup":
        asyncio.run(phase_cleanup())
        return 0
    if args.phase == "selfmsg":
        asyncio.run(phase_selfmsg())
        return 0
    if args.phase == "sweep":
        asyncio.run(phase_sweep())
        return 0
    if args.phase == "inject":
        return asyncio.run(phase_inject())
    return 1


if __name__ == "__main__":
    sys.exit(main())
