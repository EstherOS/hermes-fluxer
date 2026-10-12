#!/usr/bin/env bash
# Sandbox gateway status + last log lines.
set -u
REPO=/home/agent/.hermes/hermes-agent
SANDBOX=/home/agent/workspace/fluxer/sandbox
HOME_S="$SANDBOX/hermes-home"
HERMES_HOME="$HOME_S" "$REPO/venv/bin/python" "$REPO/hermes" gateway status
echo '--- last log lines ---'
tail -n 30 "$SANDBOX/gw.log" 2>/dev/null || true
