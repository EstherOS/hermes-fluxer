#!/usr/bin/env bash
# Stage the fluxer plugin for the LIVE gateway. Does NOT restart anything.
set -eu
SRC=/home/agent/workspace/fluxer/plugins/hermes-fluxer/src/fluxer
DEST=/home/agent/.hermes/plugins/fluxer
REPO=/home/agent/.hermes/hermes-agent
PY="$REPO/venv/bin/python"

echo "== stop sandbox (if running) =="
bash /home/agent/workspace/fluxer/scripts/sandbox_stop.sh 2>/dev/null || true

echo "== sync plugin -> $DEST =="
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$SRC"/. "$DEST"/
find "$DEST" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "== enable plugin =="
HERMES_HOME=/home/agent/.hermes "$PY" "$REPO/hermes" plugins enable fluxer-platform

echo "== verify =="
HERMES_HOME=/home/agent/.hermes "$PY" "$REPO/hermes" plugins list --user | grep -i fluxer || true
grep -q '^FLUXER_BOT_TOKEN=' /home/agent/.hermes/.env && echo "FLUXER_BOT_TOKEN: set"
grep -q '^FLUXER_ALLOWED_USERS=' /home/agent/.hermes/.env && echo "FLUXER_ALLOWED_USERS: set"

echo
echo "Staged. To activate: restart the container from the HOST (podman restart hermes /"
echo "systemctl --user restart hermes.service), then check 'hermes gateway status'."
