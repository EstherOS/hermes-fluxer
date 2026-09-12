#!/usr/bin/env python3
"""List recent messages in a Fluxer channel (read-only; cleanup checks).

Usage:
    list_recent.py <channel_id> [limit]

Reads FLUXER_BOT_TOKEN from /home/agent/workspace/fluxer/.env (never printed).
Run with the Hermes venv python.
"""
import asyncio
import re
import sys
from pathlib import Path

import aiohttp

ENV = Path("/home/agent/workspace/fluxer/.env")


def token() -> str:
    for line in ENV.read_text().splitlines():
        m = re.match(r"FLUXER_BOT_TOKEN=(.*)", line)
        if m:
            return m.group(1).strip()
    raise SystemExit("FLUXER_BOT_TOKEN not found in .env")


async def main() -> None:
    channel = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token()}"}) as s:
        async with s.get(
            f"https://api.fluxer.app/v1/channels/{channel}/messages", params={"limit": limit}
        ) as r:
            data = await r.json()
    if isinstance(data, dict):
        print("error:", data)
        return
    for m in data:
        a = m.get("author", {})
        atts = len(m.get("attachments") or [])
        print(m.get("id"), a.get("username"), f"atts={atts}", repr((m.get("content") or "")[:100]))


if __name__ == "__main__":
    asyncio.run(main())
