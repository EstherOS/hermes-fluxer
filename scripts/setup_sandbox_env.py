#!/usr/bin/env python3
"""Copy only the env values the sandbox gateway needs from the live .env.

Never prints values. Safe to re-run (idempotent merge).
"""
import os
import re
from pathlib import Path

LIVE = Path("/home/agent/.hermes/.env")
DEST = Path("/home/agent/workspace/fluxer/sandbox/hermes-home/.env")
KEYS = {"FLUXER_BOT_TOKEN", "DEEPSEEK_API_KEY"}
# Sandbox-static env vars (not from the live .env).
STATIC = {
    "FLUXER_HOME_CHANNEL": "1547830836479393792",
    "FLUXER_HOME_CHANNEL_NAME": "kairo-dm",
    "HERMES_GATEWAY_BUSY_ACK_ENABLED": "false",
}

live = LIVE.read_text()
want = {}
for line in live.splitlines():
    m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
    if m and m.group(1) in KEYS:
        want[m.group(1)] = m.group(2)

existing = {}
if DEST.exists():
    for line in DEST.read_text().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            existing[m.group(1)] = m.group(2)
existing.update(want)
existing.update(STATIC)  # static wins over any prior value
DEST.write_text("".join(f"{k}={v}\n" for k, v in sorted(existing.items())))
os.chmod(DEST, 0o600)
print("sandbox .env keys present:", sorted(existing))
missing = KEYS - set(existing)
print("missing:", sorted(missing) if missing else "none")
