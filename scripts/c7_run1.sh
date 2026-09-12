#!/bin/bash
# C7 wave 6 — evidence run 1: Mini-Omni2 stock vision inference (headless)
# Runs the repo's own inference_vision.py (preset audio+image sample),
# sampling VRAM every second. All output under status/c7-evidence/.
set -u
FLUXER=/home/agent/workspace/fluxer
cd "$FLUXER/mini-omni2"
export PYTHONUNBUFFERED=1
export HF_HUB_DISABLE_TELEMETRY=1

EV="$FLUXER/status/c7-evidence"
VRAM="$EV/run1-vram.csv"
echo "timestamp,mem_used_mib,gpu_util_pct" > "$VRAM"
( for i in $(seq 1 900); do
    nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits >> "$VRAM" 2>/dev/null
    sleep 1
  done ) &
SAMPLER=$!

echo "=== start $(date -u +%FT%TZ) ==="
START=$(date +%s)
nice -n 10 "$FLUXER/.venv-torch/bin/python" inference_vision.py \
    > "$EV/run1-vision-stock.log" 2>&1
RC=$?
END=$(date +%s)
kill $SAMPLER 2>/dev/null
echo "=== end $(date -u +%FT%TZ) rc=$RC wall=$((END-START))s ==="
exit $RC
