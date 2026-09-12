# Small multimodal model tiers (≤4B) — recommendations from Kairo

Source: kairo, Discord DM, 2026-09-11 (originally a deepseek.com chat output).
Status: candidate list for the **omni engine duplex slot** — use whichever works;
validate before trusting. Follow-up: `docs/omni-models-feasibility.md` (feasibility scout).

Tiers:
- **Jarvis**: 4-in / 2-out, simultaneous, omnimodal, agentic. → no ≤4B match.
- **Slow Jarvis**: 4-in / 2-out, turn-based, agentic. → no ≤4B match.
- **Glados**: 4-in, no audio out; pair with TTS, agentic. → no ≤4B match.
- **Pretend Jarvis**: 4-in / 2-out, not agentic. → actual candidates:

| Tier | Model | RAM (rec. quant) + ctx | Ins | Outs | Notes |
|---|---|---|---|---|---|
| Pretend Jarvis | **InteractiveOmni-4B** | ~2.5–3.0 GB (Q4_K_M) + ~0.5–1.0 GB ctx (4K–8K est.) | text, audio, image, video | text, speech | Simultaneous full-duplex. Not agentic. "Fits exactly." |
| Pretend Jarvis | **Mini-Omni2** | ~1.0 GB (Q4_K) + ~0.5 GB ctx | text, audio, image (video via frames, not native) | text, speech | Very cheap. |
| Pretend Jarvis | **Aero Realtime-4B** | ~3.0–3.5 GB (Q4_K_M) + ~0.5 GB ctx | video, audio, text | text, audio | 5B — over ≤4B; stretch only. |

No ≤4B model is natively agentic with these modality sets — the agentic part stays on
Hermes (thinker/talker split), which is exactly what our stitched profile does. The
above fill the "senses" slot (duplex backend) while Hermes stays the agent.

Kairo: "you can use whatever you want — this is just a suggestion."

## Validation results (night shift, 2026-09-11)

See `docs/omni-models-feasibility.md` for the full scout report. Short version: **none
of the three ships a quantized artifact** — the listed Q4 sizes don't correspond to real
files (true sizes: 11.8 GB / 3.5 GB / 9.7 GB BF16; no GGUF exists for any). InteractiveOmni
needs ≥12 GB + FlashAttention (Ampere+); Aero needs vLLM (sm_70+, Pascal excluded);
**Mini-Omni2 is the one plausible future candidate on this box** (torch path, ~6 GB
downloads, ~2–3 GB VRAM est.).

What runs *tonight* instead — a fully local GGUF cascade covering all four senses with
what's installed: **Qwen3-ASR** (GPU) audio-in · **Qwen3-TTS** (GPU, voice cloning)
audio-out · **SmolVLM2** image/video · llama.cpp LLM text · whisper.cpp/piper as CPU
fallbacks. Next bandwidth purchase if wanted: Qwen2.5-Omni-3B GGUF (~3.4 GB, audio+vision
in → text out) — exact commands in the scout doc §5.
