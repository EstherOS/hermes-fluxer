#!/usr/bin/env bash
# Start the sandbox gateway (fluxer dev). Never touches the live gateway (PID 1).
# Ensures the GPU ASR server is running before the gateway comes up.
set -u
REPO=/home/agent/.hermes/hermes-agent
PY="$REPO/venv/bin/python"
SANDBOX=/home/agent/workspace/fluxer/sandbox
HOME_S="$SANDBOX/hermes-home"
mkdir -p "$SANDBOX"
# refuse if already running
if [ -f "$HOME_S/gateway.pid" ] && kill -0 "$(python3 - <<'EOF' 2>/dev/null || true
import json
try:
    print(json.load(open("/home/agent/workspace/fluxer/sandbox/hermes-home/gateway.pid"))["pid"])
except Exception:
    pass
EOF
)" 2>/dev/null; then echo "sandbox gateway appears to be running already"; exit 1; fi
# ensure the ASR server is warm before gateway connects
bash /home/agent/workspace/fluxer/scripts/asr_server_start.sh
cd "$SANDBOX"
env -i HOME=/home/agent \
    PATH="$REPO/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 \
    HERMES_HOME="$HOME_S" \
    "$PY" "$REPO/hermes" gateway run -v > "$SANDBOX/gw.log" 2>&1 &
echo "started (shell pid $!); log: $SANDBOX/gw.log"
