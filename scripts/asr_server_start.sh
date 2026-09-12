#!/usr/bin/env bash
# Start the GPU Qwen3-ASR server (llama-server, Vulkan, nice 10).
# Idempotent: health-check the port before starting; refuse a duplicate.
set -u
REPO=/home/agent/workspace/fluxer
ASR_PORT=8105
HDLR=/dev/null
LOG="$REPO/status/asr-server.log"
PID_FILE="$REPO/status/asr-server.pid"
MODEL="$REPO/models/omni/qwen3-asr-0.6b/Qwen3-ASR-0.6B-Q8_0.gguf"
MMPROJ="$REPO/models/omni/qwen3-asr-0.6b/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"
LLAMA_SRV="$REPO/gpu/tools/llama-b10903/llama-server"

# Check if already listening
if command -v curl &>/dev/null; then
    if curl -sf "http://127.0.0.1:${ASR_PORT}/v1/models" >/dev/null 2>&1; then
        echo "ASR server already listening on port ${ASR_PORT} — nothing to do."
        exit 0
    fi
fi
if [ -f "$PID_FILE" ]; then
    OLD=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        echo "ASR server (pid $OLD) still running; skip."
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
if [ ! -f "$MMPROJ" ]; then
    echo "FATAL: mmproj not found at $MMPROJ"
    exit 1
fi

# Source GPU env (Vulkan ICD, EGL)
. "$REPO/gpu/gpu-env.sh"

mkdir -p "$(dirname "$LOG")"
echo "Starting ASR server on port ${ASR_PORT} (llama-server Vulkan, nice 10)..."
nohup nice -n 10 "$LLAMA_SRV" \
    -m "$MODEL" \
    --mmproj "$MMPROJ" \
    -c 2048 \
    --port "${ASR_PORT}" \
    --host 127.0.0.1 \
    -ub 64 -b 128 \
    > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"
echo "ASR server started (pid ${PID}); log: ${LOG}"

# Wait for readiness (poll /v1/models up to 30s)
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${ASR_PORT}/v1/models" >/dev/null 2>&1; then
        echo "ASR server ready in ${i}s."
        exit 0
    fi
    sleep 1
done
echo "WARNING: ASR server started but not yet responding after 30s — check ${LOG}"
exit 0