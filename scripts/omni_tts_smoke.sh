#!/usr/bin/env bash
# ============================================================================
# omni_tts_smoke.sh — text -> speech smoke test, all-GGUF, GPU (Vulkan)
#
# Model : Qwen3-TTS-12Hz-1.7B-Base Q4_K_M + mmproj Q8_0 (official ggml-org
#         GGUFs, ~1.41 GB total). Base = voice-cloning variant: a reference
#         audio file conditions the voice (--tts-speaker-file).
# Runtime: llama.cpp b10903 `llama-tts` (libmtmd TTS pipeline -> raw 24 kHz WAV).
#
# Why: local audio-out without piper; quality/voice-clone class TTS. Verified
# 2026-09-11 on the GTX 1060 6GB: 6.40 s of speech in 11.82 s (0.54x realtime)
# at -n 80; ~2.5 GB VRAM peak; whisper.cpp transcribed the output correctly
# ("Hello from fluxer, local speech on the GTX 1060"). Evidence:
# docs/captures/omni-tts-smoke.log, omni-tts-vram.log, models/omni/out/tts-smoke.wav
#
# Usage:  scripts/omni_tts_smoke.sh ["text"] [out.wav] [speaker.wav]
#
# Gotcha: keep -c small. With the model default ctx the loader reserved a
# ~917 MB buffer and failed (ErrorOutOfDeviceMemory) on this 6 GB card; -c 1024
# works. -n = number of 12 Hz frames (n/12 seconds of audio, roughly).
# ============================================================================
set -euo pipefail

ROOT=/home/agent/workspace/fluxer
cd "$ROOT"
. gpu/gpu-env.sh

LLAMA="$ROOT/gpu/tools/llama-b10903"
MODEL="$ROOT/models/omni/qwen3-tts-1.7b/Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf"
MMPROJ="$ROOT/models/omni/qwen3-tts-1.7b/mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf"

TEXT="${1:-Hello from Fluxer. This speech was generated locally on the GTX 1060.}"
OUT="${2:-$ROOT/models/omni/out/tts-smoke.wav}"
SPEAKER="${3:-$ROOT/models/jfk.wav}"   # any clean 5-15 s wav/mp3 = voice prompt

if [ ! -f "$MODEL" ] || [ ! -f "$MMPROJ" ]; then
  echo "missing model files under models/omni/qwen3-tts-1.7b/ — see docs/omni-models-feasibility.md §5" >&2
  exit 2
fi
mkdir -p "$(dirname "$OUT")"

timeout 170 nice -n 10 "$LLAMA/llama-tts" \
  -m "$MODEL" -mm "$MMPROJ" \
  -p "$TEXT" \
  --tts-lang en \
  --tts-speaker-file "$SPEAKER" \
  -o "$OUT" \
  -n 300 -ub 64 -b 128 -c 1024

echo "wrote: $OUT"; ls -la "$OUT"
