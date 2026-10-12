#!/usr/bin/env bash
# ============================================================================
# omni_asr_smoke.sh — speech -> text smoke test, all-GGUF, GPU (Vulkan)
#
# Model : Qwen3-ASR-0.6B Q8_0 + mmproj (official ggml-org GGUFs, ~972 MB total)
# Runtime: llama.cpp b10903 llama-mtmd-cli (Vulkan build already in this repo)
#
# Why: replaces/augments whisper.cpp (CPU) with a GPU audio-in path that is the
# same runtime family as the rest of the stack. Verified 2026-09-11 on the
# GTX 1060 6GB: jfk.wav transcribed exactly; 1527 MiB VRAM peak, 81% GPU util.
# Evidence: docs/captures/omni-asr-smoke.log
#
# Usage:  scripts/omni_asr_smoke.sh [audio.wav] [prompt]
# Default: models/jfk.wav, "Transcribe the speech in this audio."
#
# Flags below are the *proven-safe 6GB profile*: keep -c small (-c 1024).
# With the model's default context, llama.cpp tried to reserve a ~917 MB
# Vulkan buffer and failed with ErrorOutOfDeviceMemory on this card.
# ============================================================================
set -euo pipefail

ROOT=/home/agent/workspace/fluxer
cd "$ROOT"
. gpu/gpu-env.sh

LLAMA="$ROOT/gpu/tools/llama-b10903"
MODEL="$ROOT/models/omni/qwen3-asr-0.6b/Qwen3-ASR-0.6B-Q8_0.gguf"
MMPROJ="$ROOT/models/omni/qwen3-asr-0.6b/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"

AUDIO="${1:-$ROOT/models/jfk.wav}"
PROMPT="${2:-Transcribe the speech in this audio.}"

if [ ! -f "$MODEL" ] || [ ! -f "$MMPROJ" ]; then
  echo "missing model files under models/omni/qwen3-asr-0.6b/ — see docs/omni-models-feasibility.md §5" >&2
  exit 2
fi

timeout 170 nice -n 10 "$LLAMA/llama-mtmd-cli" \
  -m "$MODEL" --mmproj "$MMPROJ" \
  --audio "$AUDIO" \
  -p "$PROMPT" \
  -n 256 -ub 64 -b 128 -c 1024
