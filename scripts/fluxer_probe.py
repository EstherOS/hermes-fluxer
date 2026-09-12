#!/usr/bin/env python3
"""fluxer_probe.py — reusable Fluxer bot probe (REST + WebSocket gateway).

Part of the Hermes fluxer platform-integration research lane.
See docs/fluxer-api-notes.md for the full API reference and citations.

Subcommands
-----------
    listen [seconds]                 Connect the gateway, IDENTIFY, print every
                                     dispatch and save raw captures to
                                     docs/captures/ (default 90 seconds)
    send <channel_id> <text>         Send a text message (POST /channels/{id}/messages)
    send-file <channel_id> <path> [text]
                                     Upload a file via the presigned-attachment
                                     flow and send it with a message
    edit <channel_id> <message_id> <text>
                                     PATCH the message content
    delete <channel_id> <message_id> DELETE the message

Auth
----
Reads FLUXER_BOT_TOKEN from an env file, in this order:
    1. $FLUXER_ENV_FILE (if set)
    2. <repo>/.env  (repo = parent of this script's directory, i.e. ../.env)
    3. ./.env in the current directory
The token is never printed. REST calls add the `Bot ` prefix; the Gateway
IDENTIFY sends the raw token with no prefix (per docs.fluxer.app/gateway).

Environment knobs
-----------------
    FLUXER_API_BASE=https://api.fluxer.app/v1   REST base override
    FLUXER_GATEWAY_URL=wss://gateway.fluxer.app  Gateway override (else /gateway/bot)
    FLUXER_IGNORED_EVENTS=EVENT1,EVENT2          IDENTIFY ignored_events list
    FLUXER_CAPTURE_DIR=<dir>                     listen capture output dir

Usage (venv python):
    /home/agent/.hermes/hermes-agent/venv/bin/python scripts/fluxer_probe.py listen 90
    ... send 1547815091221561347 "hello from the probe"
    ... send-file 1547815091221561347 /tmp/pixel.png "caption"
    ... edit 1547815091221561347 154782... "new text"
    ... delete 1547815091221561347 154782...
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import random
import sys
import time
from pathlib import Path

import aiohttp
import websockets

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
DEFAULT_API_BASE = "https://api.fluxer.app/v1"
DEFAULT_GATEWAY = "wss://gateway.fluxer.app"
DEFAULT_CAPTURE_DIR = REPO_DIR / "docs" / "captures"
GATEWAY_QUERY = "?v=1&encoding=json"  # zstd-stream optional; JSON keeps the probe simple


# --------------------------------------------------------------------------
# env / token loading
# --------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def load_token() -> str:
    candidates: list[Path] = []
    if os.environ.get("FLUXER_ENV_FILE"):
        candidates.append(Path(os.environ["FLUXER_ENV_FILE"]))
    candidates += [REPO_DIR / ".env", Path.cwd() / ".env"]
    for cand in candidates:
        env = read_env_file(cand)
        tok = env.get("FLUXER_BOT_TOKEN") or os.environ.get("FLUXER_BOT_TOKEN")
        if tok:
            return tok
    tok = os.environ.get("FLUXER_BOT_TOKEN")
    if not tok:
        sys.exit("FLUXER_BOT_TOKEN not found (checked $FLUXER_ENV_FILE, ../.env, ./.env, env)")
    return tok


def api_base() -> str:
    return os.environ.get("FLUXER_API_BASE", DEFAULT_API_BASE).rstrip("/")


# --------------------------------------------------------------------------
# REST helpers
# --------------------------------------------------------------------------

class Rest:
    def __init__(self, token: str):
        self._auth = {"Authorization": f"Bot {token}"}

    async def request(self, method: str, path: str, *, json_body=None,
                      raw_body: bytes | None = None,
                      content_type: str | None = None) -> tuple[int, object]:
        url = f"{api_base()}{path}"
        headers = dict(self._auth)
        if content_type:
            headers["Content-Type"] = content_type
        kwargs = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if raw_body is not None:
            kwargs["data"] = raw_body
        async with aiohttp.ClientSession() as sess:
            async with sess.request(method, url, **kwargs) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text) if text else None
                except json.JSONDecodeError:
                    payload = text
                return resp.status, payload

    async def put_raw(self, url: str, data: bytes, content_type: str | None = None) -> int:
        """PUT bytes to a presigned upload URL (relay or direct storage). No auth header."""
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        async with aiohttp.ClientSession() as sess:
            async with sess.put(url, data=data, headers=headers) as resp:
                await resp.read()
                return resp.status


def jprint(label: str, obj: object) -> None:
    print(f"[{label}] {json.dumps(obj, ensure_ascii=False)[:1500]}")


def http_exit(status: int, payload: object, ok: int = 200) -> None:
    if status != ok:
        print(f"HTTP {status}: {json.dumps(payload)[:800]}", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------
# gateway listen
# --------------------------------------------------------------------------

IDENTIFY_PROPERTIES = {"os": "Linux", "browser": "hermes-fluxer-probe", "device": "hermes"}


async def gateway_url() -> str:
    if os.environ.get("FLUXER_GATEWAY_URL"):
        return os.environ["FLUXER_GATEWAY_URL"]
    try:
        rest = Rest(load_token())
        status, payload = await rest.request("GET", "/gateway/bot")
        if status == 200 and isinstance(payload, dict) and payload.get("url"):
            return payload["url"]
    except Exception as exc:  # fall back to the known-good default
        print(f"warning: /gateway/bot failed ({exc}); using {DEFAULT_GATEWAY}")
    return DEFAULT_GATEWAY


class Listener:
    def __init__(self, token: str, capture_dir: Path, seconds: float):
        self.token = token          # raw token for IDENTIFY (no "Bot " prefix)
        self.capture_dir = capture_dir
        self.deadline = time.monotonic() + seconds
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.seq: int = 0
        self.acks = 0
        self.counts: dict[str, int] = {}
        self.events_file = None
        self.started = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    def save(self, payload: dict) -> None:
        t = payload.get("t") or f"OP{payload.get('op')}"
        s = payload.get("s")
        name = f"event-{self.started}-{s if s is not None else 'x'}-{t}.json"
        (self.capture_dir / name).write_text(json.dumps(payload, indent=1))
        self.events_file.write(json.dumps(payload) + "\n")
        self.events_file.flush()

    def identify_payload(self) -> dict:
        d: dict = {"token": self.token, "properties": IDENTIFY_PROPERTIES}
        extra = os.environ.get("FLUXER_IDENTIFY_EXTRA")
        if extra:
            try:
                merged = json.loads(extra)
                if isinstance(merged, dict):
                    d.update(merged)
            except json.JSONDecodeError:
                print("warning: FLUXER_IDENTIFY_EXTRA is not valid JSON; ignored")
        if os.environ.get("FLUXER_IGNORED_EVENTS"):
            d["ignored_events"] = [x.strip().upper() for x in
                                   os.environ["FLUXER_IGNORED_EVENTS"].split(",") if x.strip()]
        return {"op": 2, "d": d, "s": None, "t": None}

    async def heartbeat_loop(self, ws, interval_ms: int) -> None:
        interval = interval_ms / 1000.0
        await asyncio.sleep(interval * random.random())  # first beat with jitter
        while True:
            await ws.send(json.dumps({"op": 1, "d": self.seq if self.seq else None}))
            await asyncio.sleep(interval)

    async def run_once(self, ws, hb_task_holder: list) -> str:
        """Consume frames; return 'done' when the time budget is spent,
        'reconnect' when the server asks, or raise on socket loss."""
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                return "done"
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                continue
            payload = json.loads(raw)
            op = payload.get("op")
            if op == 0:
                if payload.get("s") is not None:
                    self.seq = payload["s"]
                t = payload.get("t", "?")
                self.counts[t] = self.counts.get(t, 0) + 1
                if t == "READY":
                    d = payload.get("d") or {}
                    self.session_id = d.get("session_id")
                    self.resume_url = d.get("resume_gateway_url") or self.resume_url
                self.save(payload)
                print(f"[dispatch s={payload.get('s')}] {t} "
                      f"{json.dumps(payload.get('d'), ensure_ascii=False)[:1000]}")
            elif op == 10:
                interval = payload["d"]["heartbeat_interval"]
                print(f"[hello] heartbeat_interval={interval}ms; identifying "
                      f"(ignored_events={os.environ.get('FLUXER_IGNORED_EVENTS', '') or 'none'})")
                await ws.send(json.dumps(self.identify_payload()))
                hb_task_holder.append(asyncio.create_task(self.heartbeat_loop(ws, interval)))
            elif op == 11:
                self.acks += 1
            elif op == 1:
                await ws.send(json.dumps({"op": 1, "d": self.seq if self.seq else None}))
            elif op == 7:
                print("[reconnect] server requested reconnect")
                return "reconnect"
            elif op == 9:
                print(f"[invalid-session] resumable={payload.get('d')}")
                if not payload.get("d"):
                    self.session_id = None
                    self.seq = 0
            if time.monotonic() >= self.deadline:
                return "done"
        return "done"

    async def run(self) -> None:
        url = await gateway_url()
        full = url + GATEWAY_QUERY
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.events_file = (self.capture_dir / f"listen-{self.started}.jsonl").open("a")
        attempt = 0
        while time.monotonic() < self.deadline:
            try:
                target = (self.resume_url or url) + GATEWAY_QUERY
                print(f"[connect] {target} (attempt {attempt + 1})")
                async with websockets.connect(target, max_size=2 ** 24) as ws:
                    hb: list = []
                    reason = await self.run_once(ws, hb)
                    for task in hb:
                        task.cancel()
                    if reason == "done":
                        break
                if self.session_id:
                    # resume on a fresh socket
                    target = (self.resume_url or url) + GATEWAY_QUERY
                    async with websockets.connect(target, max_size=2 ** 24) as ws:
                        await ws.send(json.dumps({"op": 6, "d": {
                            "token": self.token,
                            "session_id": self.session_id,
                            "seq": self.seq}}))
                        hb2: list = []
                        reason = await self.run_once(ws, hb2)
                        for task in hb2:
                            task.cancel()
                        if reason == "done":
                            break
            except (websockets.ConnectionClosed, OSError) as exc:
                print(f"[socket] closed: {exc}")
            attempt += 1
            if attempt >= 5 or time.monotonic() >= self.deadline:
                break
            backoff = min(1 + attempt, 10) + random.random()
            print(f"[reconnect] retrying in {backoff:.1f}s")
            await asyncio.sleep(backoff)
        if self.events_file:
            self.events_file.close()
        print(f"[done] acks={self.acks} events={self.counts}")
        print(f"[done] captures in {self.capture_dir}")


# --------------------------------------------------------------------------
# message ops
# --------------------------------------------------------------------------

async def cmd_send(rest: Rest, channel_id: str, text: str) -> None:
    status, payload = await rest.request(
        "POST", f"/channels/{channel_id}/messages", json_body={"content": text})
    http_exit(status, payload)
    print(f"[sent] id={payload.get('id')} channel={channel_id}")
    jprint("message", payload)


async def _upload_one(rest: Rest, channel_id: str, path: Path) -> dict:
    """Plan + transfer (+ complete) one attachment. Returns the claim payload."""
    data = path.read_bytes()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    decl = {"id": 0, "filename": path.name, "file_size": len(data), "content_type": ctype}
    status, plan = await rest.request(
        "POST", f"/channels/{channel_id}/attachments",
        json_body={"attachments": [decl]})
    http_exit(status, plan)
    item = plan["attachments"][0]
    print(f"[upload-plan] mode={item['upload_mode']} size={len(data)} type={ctype}")
    if item["upload_mode"] == "singlepart":
        put_status = await rest.put_raw(item["upload_url"], data, ctype)
        if put_status not in (200, 201, 204):
            sys.exit(f"singlepart PUT failed: HTTP {put_status}")
        print(f"[upload-put] singlepart ok (HTTP {put_status})")
        upload_filename = item["upload_filename"]
    else:
        part_size = item["part_size"]
        for part in sorted(item["parts"], key=lambda p: p["part_number"]):
            num = part["part_number"]
            chunk = data[(num - 1) * part_size: num * part_size]
            put_status = await rest.put_raw(part["upload_url"], chunk)
            if put_status not in (200, 201, 204):
                sys.exit(f"multipart part {num} PUT failed: HTTP {put_status}")
        print(f"[upload-put] multipart {len(item['parts'])} parts ok")
        status, done = await rest.request(
            "POST", f"/channels/{channel_id}/attachments/complete",
            json_body={"uploads": [{"upload_filename": item["upload_filename"],
                                    "upload_id": item["upload_id"]}]})
        http_exit(status, done)
        upload_filename = done["uploads"][0]["upload_filename"]
        print(f"[upload-complete] {upload_filename}")
    return {"id": 0, "filename": path.name, "upload_filename": upload_filename,
            "file_size": len(data), "content_type": ctype}


async def cmd_send_file(rest: Rest, channel_id: str, path_str: str, text: str) -> None:
    path = Path(path_str)
    if not path.is_file():
        sys.exit(f"file not found: {path}")
    claim = await _upload_one(rest, channel_id, path)
    status, payload = await rest.request(
        "POST", f"/channels/{channel_id}/messages",
        json_body={"content": text, "attachments": [claim]})
    http_exit(status, payload)
    print(f"[sent-file] id={payload.get('id')}")
    jprint("message", payload)
    for att in payload.get("attachments") or []:
        print(f"[attachment] id={att.get('id')} url={att.get('url')} "
              f"proxy_url={att.get('proxy_url')} expired={att.get('expired')} "
              f"expires_at={att.get('expires_at')}")


async def cmd_edit(rest: Rest, channel_id: str, message_id: str, text: str) -> None:
    status, payload = await rest.request(
        "PATCH", f"/channels/{channel_id}/messages/{message_id}",
        json_body={"content": text})
    http_exit(status, payload)
    print(f"[edited] id={payload.get('id')} edited_timestamp={payload.get('edited_timestamp')}")
    jprint("message", payload)


async def cmd_delete(rest: Rest, channel_id: str, message_id: str) -> None:
    status, payload = await rest.request(
        "DELETE", f"/channels/{channel_id}/messages/{message_id}")
    if status not in (200, 204):
        http_exit(status, payload)
    print(f"[deleted] id={message_id} (HTTP {status})")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def usage() -> None:
    print(__doc__)
    sys.exit(2)


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        usage()
    token = load_token()
    cmd, *rest_args = argv
    rest = Rest(token)

    async def runner() -> None:
        if cmd == "listen":
            seconds = float(rest_args[0]) if rest_args else 90.0
            cap = Path(os.environ.get("FLUXER_CAPTURE_DIR", DEFAULT_CAPTURE_DIR))
            await Listener(token, cap, seconds).run()
        elif cmd == "send" and len(rest_args) == 2:
            await cmd_send(rest, rest_args[0], rest_args[1])
        elif cmd == "send-file" and len(rest_args) >= 2:
            text = rest_args[2] if len(rest_args) > 2 else ""
            await cmd_send_file(rest, rest_args[0], rest_args[1], text)
        elif cmd == "edit" and len(rest_args) == 3:
            await cmd_edit(rest, rest_args[0], rest_args[1], rest_args[2])
        elif cmd == "delete" and len(rest_args) == 2:
            await cmd_delete(rest, rest_args[0], rest_args[1])
        else:
            usage()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
