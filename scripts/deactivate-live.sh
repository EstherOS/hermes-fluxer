#!/usr/bin/env bash
# Disable the fluxer plugin on the live gateway. Needs a container restart to unload.
set -u
REPO=/home/agent/.hermes/hermes-agent
PY="$REPO/venv/bin/python"

HERMES_HOME=/home/agent/.hermes "$PY" "$REPO/hermes" plugins disable fluxer-platform || true
if [ -d /home/agent/.hermes/plugins/fluxer ]; then
  mv /home/agent/.hermes/plugins/fluxer /home/agent/.hermes/plugins/fluxer.disabled.$(date +%s)
fi
echo "Disabled + moved aside. Restart the container to unload the lane."
