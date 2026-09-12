#!/usr/bin/env python3
"""Live selftest for the fluxer plugin protocol clients (C1 evidence).

Exercises the real Fluxer API + gateway with the bot token from
``/home/agent/workspace/fluxer/.env``:

1. REST ``get_me`` (prints username);
2. gateway connect → READY (prints bot user id / session id);
3. send ONE ``[selftest] fluxer client ok <UTC ts>`` message to #general;
4. capture its own MESSAGE_CREATE from the live event stream;
5. edit the message (and capture MESSAGE_UPDATE);
6. send one ~2500-char message to probe the 4000-char bot limit claim
   (records the server's answer; never retry-loops);
7. delete every message it created;
8. disconnect and print a PASS/FAIL summary.

All test messages are prefixed ``[selftest]`` and are deleted before exit.
The token is read from .env and never printed.

Run (bounded, nice'd)::

    nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/client_selftest.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
CHECKOUT = Path("/home/agent/.hermes/hermes-agent")
PLUGIN_SRC = REPO / "plugin-src"
ENV_FILE = REPO / ".env"
DEFAULT_CHANNEL_ID = "1547815091221561347"  # #general on the research guild
STEP_TIMEOUT_S = 60.0
GATEWAY_FALLBACK_URL = "wss://gateway.fluxer.app"


def _ensure_imports() -> None:
    """Make ``fluxer.rest`` / ``fluxer.gatewayws`` importable.

    The package ``__init__`` imports the adapter (owned by another coder in
    the same wave); if that import fails we install a path-only package stub
    so this selftest only depends on the protocol clients.
    """
    for path in (str(CHECKOUT), str(PLUGIN_SRC)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import fluxer.rest  # noqa: F401
        import fluxer.gatewayws  # noqa: F401

        return
    except Exception as exc:
        print(f"[selftest] fluxer package import failed ({exc!r}); using path-only stub")
    for name in [n for n in list(sys.modules) if n == "fluxer" or n.startswith("fluxer.")]:
        sys.modules.pop(name, None)
    stub = types.ModuleType("fluxer")
    stub.__path__ = [str(PLUGIN_SRC / "fluxer")]
    sys.modules["fluxer"] = stub


_ensure_imports()

from fluxer.rest import FluxerAPIError, FluxerREST  # noqa: E402
from fluxer.gatewayws import FluxerGatewayClient  # noqa: E402


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.results)


async def wait_for_event(queue: asyncio.Queue, event_type: str, message_id: str, timeout: float):
    """Wait for a matching dispatch event; returns the payload or None."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            event, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if event == event_type and payload.get("id") == message_id:
            return payload


async def run(checks: Checks) -> int:
    env = read_env_file(ENV_FILE)
    token = env.get("FLUXER_BOT_TOKEN") or os.environ.get("FLUXER_BOT_TOKEN")
    channel_id = env.get("FLUXER_GENERAL_CHANNEL_ID") or DEFAULT_CHANNEL_ID

    if not token:
        checks.add("env: FLUXER_BOT_TOKEN", False, f"missing from {ENV_FILE}")
        return 1
    checks.add("env: FLUXER_BOT_TOKEN", True, f"loaded from {ENV_FILE} (len={len(token)}, value not printed)")
    print(f"[selftest] channel={channel_id}")

    queue: asyncio.Queue = asyncio.Queue()
    conn_events: list[tuple[str, object]] = []

    async def on_event(event: str, payload: dict) -> None:
        if event in ("MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE"):
            print(f"  [ws] {event} id={payload.get('id')} channel={payload.get('channel_id')}")
            await queue.put((event, payload))

    async def on_connection_event(kind: str, payload: dict | None) -> None:
        if kind == "ready" and isinstance(payload, dict):
            summary = {
                "session_id": payload.get("session_id"),
                "user": (payload.get("user") or {}).get("id"),
            }
        else:
            summary = payload
        conn_events.append((kind, summary))
        print(f"  [ws-conn] {kind}: {summary}")

    rest = FluxerREST(token)
    client: FluxerGatewayClient | None = None
    created: list[str] = []
    deleted: list[str] = []

    async def steps() -> None:
        nonlocal client

        me = await rest.get_me()
        checks.add("REST get_me", bool(me.get("id")), f"username={me.get('username')} id={me.get('id')}")
        await asyncio.sleep(0.3)

        info = await rest.get_gateway_info()
        gateway_url = info.get("url") if isinstance(info, dict) else None
        checks.add("REST get_gateway_info", bool(gateway_url), f"url={gateway_url or GATEWAY_FALLBACK_URL}")
        await asyncio.sleep(0.3)

        client = FluxerGatewayClient(
            token,
            url=gateway_url or GATEWAY_FALLBACK_URL,
            on_event=on_event,
            on_connection_event=on_connection_event,
        )
        await client.start()
        checks.add(
            "gateway READY",
            client.user_id is not None,
            f"user_id={client.user_id} session_id={client.session_id}",
        )
        await asyncio.sleep(0.5)

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = f"[selftest] fluxer client ok {stamp}"
        message = await rest.create_message(channel_id, content=text)
        created.append(message["id"])
        checks.add("REST create_message", bool(message.get("id")), f"id={message['id']} content={text!r}")
        await asyncio.sleep(0.7)

        captured = await wait_for_event(queue, "MESSAGE_CREATE", message["id"], timeout=15)
        checks.add(
            "gateway MESSAGE_CREATE captured (own message)",
            captured is not None and captured.get("content") == text,
            f"id={message['id']} content={captured.get('content') if captured else None!r}",
        )
        await asyncio.sleep(0.5)

        edited_text = text + " (edited)"
        edited = await rest.edit_message(channel_id, message["id"], content=edited_text)
        checks.add(
            "REST edit_message",
            edited.get("edited_timestamp") is not None and edited.get("content") == edited_text,
            f"id={edited.get('id')} edited_timestamp={edited.get('edited_timestamp')}",
        )
        await asyncio.sleep(0.7)

        update_event = await wait_for_event(queue, "MESSAGE_UPDATE", message["id"], timeout=10)
        checks.add(
            "gateway MESSAGE_UPDATE captured (own edit)",
            update_event is not None and update_event.get("content") == edited_text,
            f"id={message['id']}",
        )
        await asyncio.sleep(0.5)

        # 4000-char bot limit probe: 2500 chars (content marker first so it is
        # recognisably test content no matter what happens).
        long_text = "[selftest][probe] " + ("y" * 2500)
        try:
            long_message = await rest.create_message(channel_id, content=long_text)
            created.append(long_message["id"])
            checks.add(
                "2500-char message probe",
                True,
                f"accepted (len={len(long_text)}) id={long_message['id']} — bot limit >2500 (4000 claim plausible)",
            )
        except FluxerAPIError as exc:
            detail = f"status={exc.status} code={exc.code} message={exc.message}"
            checks.add("2500-char message probe", exc.status == 400, f"rejected: {detail}")
            if exc.status != 400:
                raise
        await asyncio.sleep(1.0)

    try:
        try:
            await asyncio.wait_for(steps(), STEP_TIMEOUT_S)
        except asyncio.TimeoutError:
            checks.add("selftest steps", False, f"timed out after {STEP_TIMEOUT_S:.0f}s")
        except Exception as exc:
            checks.add("selftest steps", False, f"unhandled error: {type(exc).__name__}: {exc}")
    finally:
        for message_id in created:
            if message_id in deleted:
                continue
            try:
                await rest.delete_message(channel_id, message_id)
                deleted.append(message_id)
                print(f"  [cleanup] deleted message {message_id}")
            except Exception as exc:
                print(f"  [cleanup] failed to delete {message_id}: {exc!r}")
            await asyncio.sleep(0.5)
        if client is not None:
            try:
                await client.stop()
            except Exception as exc:
                print(f"  [cleanup] gateway stop error: {exc!r}")
        await rest.close()

    checks.add(
        "cleanup: every created message deleted",
        bool(created) and set(created) <= set(deleted),
        f"created={created} deleted={deleted}",
    )
    checks.add(
        "connection events seen",
        any(kind == "ready" for kind, _ in conn_events),
        f"kinds={[kind for kind, _ in conn_events]}",
    )
    return 0 if checks.ok else 1


def main() -> int:
    try:
        os.nice(10)
    except OSError:
        pass
    print(f"[selftest] fluxer protocol clients live check — {datetime.now(timezone.utc).isoformat()}")
    checks = Checks()
    try:
        code = asyncio.run(run(checks))
    except KeyboardInterrupt:
        print("\n[selftest] interrupted")
        return 2
    print("\n=== SELFTEST SUMMARY ===")
    for name, ok, detail in checks.results:
        print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}")
    passed = sum(1 for _, ok, _ in checks.results if ok)
    print(f"RESULT: {'PASS' if checks.ok else 'FAIL'} ({passed}/{len(checks.results)})")
    return code


if __name__ == "__main__":
    sys.exit(main())
