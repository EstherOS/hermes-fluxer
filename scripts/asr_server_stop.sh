#!/usr/bin/env bash
# Stop the ASR server (llama-server) by pid file.
set -u
PID_FILE=/home/agent/workspace/fluxer/status/asr-server.pid
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "ASR server (pid $PID) stopped."
    else
        rm -f "$PID_FILE"
        echo "PID file empty; removed."
    fi
else
    pkill -f "llama-server.*qwen3-asr" 2>/dev/null || echo "No ASR server process found."
fi