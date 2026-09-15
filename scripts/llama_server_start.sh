#!/usr/bin/env bash
# Start the brain LLM (MiniCPM5-2B, llama-server Vulkan, nice 10).
# Idempotent: health-check the port before starting; refuse a duplicate.
set -u
REPO=/home/agent/workspace/fluxer
BIN=/home/agent/workspace/fluxer-local
LLM_PORT=8085
LOG="$REPO/status/llama-server.log"
PID_FILE="$REPO/status/llama-server.pid"
MODEL="$BIN/models/MiniCPM5-2B-Q4_K_M.gguf"
LLAMA_SRV="$BIN/gpu/tools/llama-cpp-vulkan/llama-server"

# Check if already listening
if command -v curl &>/dev/null; then
    if curl -sf "http://127.0.0.1:${LLM_PORT}/health" >/dev/null 2>&1; then
        echo "llama-server already listening on port ${LLM_PORT} — nothing to do."
        exit 0
    fi
fi
if [ -f "$PID_FILE" ]; then
    OLD=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        echo "llama-server (pid $OLD) still running; skip."
        exit 0
    fi
    rm -f "$PID_FILE"
fi
if [ ! -x "$LLAMA_SRV" ]; then
    echo "FATAL: llama-server not found at $LLAMA_SRV"
    exit 1
fi
if [ ! -f "$MODEL" ]; then
    echo "FATAL: model not found at $MODEL"
    exit 1
fi

# Source GPU env (Vulkan ICD, EGL) and add the bundled ggml libs
. "$BIN/gpu-env/gpu-env.sh"
export LD_LIBRARY_PATH="$(dirname "$LLAMA_SRV"):${LD_LIBRARY_PATH:-}"

mkdir -p "$(dirname "$LOG")"
echo "Starting llama-server on port ${LLM_PORT} (MiniCPM5-2B Vulkan, nice 10)..."
nohup nice -n 10 "$LLAMA_SRV" \
    -m "$MODEL" \
    --host 127.0.0.1 \
    --port "${LLM_PORT}" \
    -c 16384 \
    --gpu-layers 99 \
    --jinja \
    > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"
echo "llama-server started (pid ${PID}); log: ${LOG}"

# Wait for readiness (poll /health up to 90s — model load takes a while)
for i in $(seq 1 90); do
    if curl -sf "http://127.0.0.1:${LLM_PORT}/health" >/dev/null 2>&1; then
        echo "llama-server ready in ${i}s."
        exit 0
    fi
    sleep 1
done
echo "WARNING: llama-server started but not yet responding after 90s — check ${LOG}"
exit 0
