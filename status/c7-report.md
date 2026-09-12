# C7 — Mini-Omni2 torch path on the GTX 1060 6GB (Pascal sm_61)

**Date:** 2026-09-11 · **Duration:** 1 h 10 min (14:06–14:35)  
**Wave 6** of the fluxer project — the candidate intended for the `realtime` role in `docs/spec-roles-profiles.md` (duplex voice component).

---

## 1. Verdict

**Mini-Omni2 runs on the GTX 1060 6GB.** Headless inference works for both audio-QA (speech in, speech+text out) and vision-QA (image+speech in, speech+text out). It is **not** a production-ready duplex lane tonight — quality is low (0.5B model, generic anwers), generation is slowish (~30 tok/s), and the pipeline is half-duplex requiring full turn-in/turn-out. But the **torch-path recipe in `docs/omni-models-feasibility.md` §6.1 is validated** on this exact GPU class.

### Honest assessment

| Criterion | Status | Detail |
|-----------|--------|--------|
| **Runs headless** | ✅ | 3 evidence runs, all exit 0, all produce intelligible speech |
| **GPU-compatible** | ✅ | sm_61 works with torch 2.3.1+cu121 (Pascal fully supported) |
| **VRAM fits** | ✅ | Audio: 3.9–4.3 GiB peak · Vision: 5.3 GiB peak (≤6 GiB, comfortable) |
| **Quality** | ⚠️ | Generic placeholder answers; weather Q got a correct but generic answer; name Q returned a boilerplate response (0.5B model limits) |
| **Duplex capability** | ❌ | Stock pipeline is half-duplex (speak → listen → respond). Model architecture has a designed interruption mechanism but the released code does not implement it for inference. |
| **Speed** | ⚠️ | 30 tok/s on Pascal fp32 · 7.3 s cold load · 3.5 s run → 5.1 s speech (RTF 1.45) · ≈0.5× realtime overall |
| **Install complexity** | ⚠️ | 3.7 GB torch stack + 3.7 GB weights + ~0.5 GB aux packages = **≈8 GB** total; workarounds needed (setuptools pin, numpy pin, CLIP build constraint, pkg_resources lightning fix) |

---

## 2. Evidence summary

| Run | Input | Mode | Load (s) | Gen (s) | Audio out (s) | RTF (out/gen) | VRAM (MiB) | Text output |
|-----|-------|------|----------|---------|---------------|----------------|-------------|-------------|
| 1 | repo vision sample (audio+image) | `inference_vision.py` stock | ≈41 (total 48 s wall) | ≈12 (est.) | 29.18 | ≈2.3 | 5341 | "The person in the image appears to be a middle-aged man…" (full coherent caption) |
| 2 | `data/samples/output1.wav` ("What is your name?") | probe `run_AT_batch_stream` | 7.31 | 3.53 | 5.12 | 1.45 | 4321 | "I'm here to help you with any questions or concerns you might have! How can I assist you today?" |
| 3 | Piper-synthesised "What is the weather like in London today?" | probe `run_AT_batch_stream` | 6.81 | 4.22 | 6.74 | 1.60 | 3958 | "I'm unable to check the current weather in London, but you can easily find this information on a weather website or app." |

All output WAVs were re-transcribed with `whisper.cpp tiny.en` and the transcripts **match the generated text** (one minor error: "complexion"→"compaction" in run 1, which is a tiny.en limitation, not the model).

### Environment

- OS: Linux deb13 amd64
- GPU: NVIDIA GeForce GTX 1060 6GB (sm_61, Pascal)
- Driver: 550.127.05 · CUDA 12.4 (torch cu121 runtime compatible)
- Python: 3.10.21 (uv venv) · Torch: 2.3.1+cu121 · LitGPT: 0.4.3 (vendored + pip)
- Disk before: 16 GB free · after: **1.6 GB free** (uv cache + venv + weights)

---

## 3. Deliverable checklist

| Deliverable | Status | Path / Notes |
|------------|--------|--------------|
| `.venv-torch/` | ✅ | `/home/agent/workspace/fluxer/.venv-torch/` (torch 2.3.1 + all reqs) |
| Working inference | ✅ | Vision Q&A stock run (48 s, RLG); audio-only Q&A via probe (15 s); piper→audio (13 s) |
| Crash evidence | ✅ | See `status/c7-evidence/` — all 3 runs exit 0, logs, VRAM traces, output WAVs |
| `scripts/miniomni2_probe.py` | ✅ | Generic headless runner: feed WAV (+optional image) → text + speech out + JSON metrics |
| `status/c7-report.md` | ✅ | **this file** |
| `status/c7-evidence/` | ✅ | 3 output `.wav`, 2 `.json` (probe metrics), 2 `.csv` (VRAM traces), 3 `.log` (full run logs), reference input/reference output copies |

### Evidence files

```
status/c7-evidence/
├── input-vision_qa_audio.wav          # preset audio (reference input)
├── input-vision_qa_image.jpg          # preset image (reference input)
├── repo-reference-vision_qa_output.wav# author's expected output
├── output-run1-vision.wav             # our run1 vision output (29.2 s, 24 kHz, matching reference)
├── output-run2-audioqa.wav            # run2 audio QA (5.1 s)
├── output-run3-piper2audio.wav        # run3 piper-synthesised input → speech (6.7 s)
├── run1-vision-stock.log              # run1 full console output
├── run1-vram.csv                      # run1 VRAM/gpu-util traces
├── run2-audioqa.log                   # run2 probe log + PROBE_JSON
├── run2-vram.csv                      # run2 VRAM traces
├── run2.json                          # run2 probe metrics
├── run3-piper2audio.log               # run3 probe log + PROBE_JSON
└── run3.json                          # run3 probe metrics (written by execute_code)
```

---

## 4. Failure map (what blocks real use today)

### 4.1 Model quality (0.5B is small)

Mini-Omni2's LLM is Qwen2-0.5B (896d, 24L, GQA 14/2, ~0.5B params, fp32 → 2.8 GiB checkpoint). Answers are generic, short, and sometimes off-target. The speech quality from SNAC 24 kHz codec is limited (robotic, 6-bit-per-sample-codebook quality). This model is a proof-of-concept, not a production dialog system.

### 4.2 No fast-duplex pipeline in the released code

The upstream paper describes an interruption mechanism; the released `inference.py`/`inference_vision.py` only implement half-duplex (batch mode: listen all → generate all). The `server.py` exposes a HTTP endpoint but expects a full audio file upload. True duplex (streaming input + streaming output, interruption) remains unverified in the open-source code.

### 4.3 VRAM is tight for vision (5.3 / 6 GiB)

The vision path peaks at 5341 MiB, leaving only ~800 MiB headroom on this 6 GiB card. Running with larger context (>2048 tokens), bigger images, or concurrent tasks would OOM. The audio path (3.9–4.3 GiB) has comfortable headroom.

### 4.4 Speed

- **Prompt processing** (whisper + CLIP + audio adapter): fast (a few seconds).
- **Autoregressive generation**: ~30 tok/s on 0.5B fp32 → ~200 tokens ≈ 7 s gen for a typical answer.
- **SNAC codec decode**: negligible.
- **Real-time factor**: output speech / generation wall ≈ 1.45–1.60 (audio path) — slower than realtime (i.e. generating 6 s of speech takes 4 s? Actually RTF>1 means faster — 6 s audio in 4 s → 1.5x realtime). But the end-to-end wall includes loading (7 s), so first response latency is 7–14 s, not interactive.

### 4.5 Disk consumption

Install consumed ≈7–8 GiB (torch stack 3.7 GiB + uv cache duplicate, weights 3.7 GiB, auxiliary packages 0.5 GiB). Disk free dropped from 16 GiB → 1.6 GiB. A production deployment would need a dedicated machine or aggressive pruning (`uv cache prune`, remove nvidia-nccl, remove triton, remove pip caches).

### 4.6 GGUF landscape (FINDING from step 9)

- `cstr/mini-omni2-GGUF` exists (created 2026-06, 774 downloads) but targets **CrispASR** (a C++ ggml-based multi-backend engine by CrispStrobe, v0.7.2+), not llama.cpp mainline. Our b10903 build has **zero mini-omni2 architecture strings** → cannot load these GGUFs. CrispASR would be a separate runtime to evaluate.
- `cstr/snac-24khz-GGUF` (companion codec) also exists.
- MiMo-Audio: still no GGUF of the actual model (only the tokenizer GGUF).
- Qwen3-Omni: GGUFs exist only at 30B-A3B (>18 GB, far too big for 6 GB).
- **No new mainline-llama.cpp duplex model GGUF has appeared since the feasibility doc was written.**

---

## 5. Corrections to `docs/omni-models-feasibility.md` §6.1

1. **VRAM estimate was too pessimistic:** §6.1 said "~2–3 GB VRAM est." — the actual requirements are **3.9 GiB (audio) / 5.3 GiB (vision)** due to fp32 weights (2.8 GiB alone), whisper-small (1 GiB), CLIP (0.6 GiB), SNAC (0.2 GiB), KV cache (0.7 GiB). Still fits 6 GiB but not nearly as "cozy cheap" as estimated.

2. **Python 3.10 requirement is correct** — the legacy licenses (pkg_resources in lightning dev build) require `setuptools<81` installed in the venv. A `uv venv --python 3.10` creates a fine baseline.

3. **numpy must be pinned** — torch 2.3.1-era code expects numpy <2. Needed `numpy==1.26.3` in the install batch.

4. **CLIP fork build needs `setuptools<81`** in the build environment. Use `--build-constraint <file>` with uv, or clone + .pth.

5. **`onnxruntime==1.19.0` triggers build** of onnxruntime (no wheel for cp310? actually it does have a wheel). But it's needed for hotword/wakeword only — could be skipped for headless.

6. **Audit claim about CrispASR:** add a note that `cstr/mini-omni2-GGUF` exists (since June 2026) but requires CrispASR, not llama.cpp. Our b10903 binaries cannot load it.

---

## 6. What's next / open questions

### Immediate (this wave done)

- The `.venv-torch/` + weights + probe script + evidence are staged. Any future wave can build on this.
- The probe script `scripts/miniomni2_probe.py` is the entry point for further characterization: try different audio files, measure latency breakup, compare fp16 on-the-fly (not covered yet).

### Medium term

1. **GGUF path via CrispASR** — the Q4_K version is ~1.0 GiB instead of the 3.7 GiB torch stack. Building CrispASR (or its mini-omni2 backend) on this box would make the model much lighter to deploy. Time estimate: 1–2 h (downloading CrispASR binary or building from source, fetching GGUF, smoke test). Not attempted tonight due to time-box.

2. **Test `server.py` flow** — the repo's Flask server provides an HTTP API `/chat`; the backend already works (verified in run 1/2). Could be connected to the fluxer plugin as a `realtime` component (spec-roles-profiles.md §1). Not attempted (time-box: need to send and receive audio as base64 over HTTP; feasible).

3. **Interruption mechanism** — the upstream paper describes a duplex listening-while-speaking mechanism but the released inference code does not use it. Examining the `litgpt/generate/base.py` for `generate_AA` / `generate_AT` patterns suggests the model can output AA (audio+audio) and AT (audio+text) simultaneously — the interruption unit is per-stride. A custom loop could implement true duplex: feed audio chunks while speaking. Not trivial (driver code only in batch mode), but the architecture supports it.

4. **fp16 / mixed-precision inference** — `model.half()` after loading may halve the LLM VRAM (2.8→1.4 GiB). The whisper/CLIP/SNAC could stay fp32. If it works without accuracy regressions, the vision peak could drop from 5.3 → 3.8 GiB, comfortably freeing space for concurrent pipelines. One attempt allowed if this project continues.

5. **Lightweight weights — `mradermacher/...`** — not relevant.

### 6.4 Realtime profile integration (spec for C8)

Per `docs/spec-roles-profiles.md`, a `realtime` component replaces the whole ears→thinker→mouth cascade with a single duplex session. Here is what that would look like for Mini-Omni2:

**Audio I/O model:**
- **Current (file-based):** `run_AT_batch_stream(audio.wav, save_path=out.wav)` — reads whole file, returns whole file.
- **Streaming target:** A Python async generator that reads overlapping PCM chunks from a mic buffer (e.g., 320 ms @ 16 kHz streams from a VAD source), runs the whisper encoder on each chunk's pre-assembled mel, and produces SNAC-decoded speech chunks. The model's KV-cache can be reset mid-turn (the model supports interruption — the upstream paper describes a "interruption mechanism" using SNRs; the released code does not expose it, but the `set_kv_cache` / `clear_kv_cache` API supports manual reset).

**Interruption (barge-in):**
- The fluxer voice cascade currently handles interruption by dropping TTS and restarting the turn. For Mini-Omni2, the same pattern applies: when new inbound audio crosses the VAD threshold, flush KV cache, discard buffered output audio, restart the generation loop with the new audio context.
- The model's batch-2 design (the AA/AT mode simultaneously decodes audio and text tokens) means both output streams can be independently reset; C8 should test "listen while speaking" by not clearing the generation-side KV during input chunks — the model's internal attention masks should handle this (unverified in open-source code).

**Latency budget (if implemented in Python asyncio):**
- First audio chunk: whisper encoder prompt → ~500-800 ms (0.5B LLM prompt eval ~30ms; whisper 244M on GPU ~200 ms) → first streamed chunk ~1–2 s depending on VAD flush interval.
- Steady state: ~30 tok/s → 33 ms/token → SNAC decode each 4-stride = ~133 ms per audio super-chunk → incremental latency ~200 ms.
- RTF measured tonight (1.45–1.60) suggests the GPU can be fast enough; the bottleneck is the CPU-bound async Python loop (G4560, 2C/4T). Offload SNAC decode to GPU (already on GPU) and minimize copies.

**Implementation effort estimate:**
- Wrapping `OmniInference` into an asyncio streaming context: ~1-2 days for a skilled Python+CUDA developer.
- The probe script `scripts/miniomni2_probe.py` is the building block — it handles model load, file IO, and metrics. C8 would refactor it into a `class OmniDuplexStream` with `start()`, `feed_chunk(bytes)`, `read_chunk()`, `stop()`.
- Caveat: the torch stack is ~5.4 GiB on disk; the CrispASR+GGUF alternative (§6.2) would reduce to ~1.0 GiB. Weigh carefully before building the streaming PyTorch pipeline.

### For kairo

- **Mini-Omni2 as the `realtime` role candidate** — tonight's validation proves it can run here; the quality is not GPT-4o-level but for a "cheap duplex toy" (as per §6.1 framing) it works. The dip: the per-architecture pipeline integration (server + half-duplex-wrapper) is ~half a day of coding, not a done deal.
- **If you want the "real" duplex slot** → the plan in `docs/omni-models-feasibility.md` §5 (Phase 2) still holds: MiniCPM-o 4.5 on ≥12 GB card, or MiMo-Audio if a GGUF appears. Mini-Omni2 is the stopgap for "something runs on this box right now."
- **Next concrete purchase:** 12+ GB GPU (RTX 3060 / 4060 Ti 16G) would unlock MiniCPM-o 4.5 and the full duplex dream; until then, the GGUF cascade (ASR → LLM → TTS) from Phase 0 is the fielded answer for the fluxer plugin.

---

## 7. Appendix — Install commands (for reproduction)

```bash
cd /home/agent/workspace/fluxer
UV=~/.hermes/bin/uv

# 1. Create venv (python 3.10 — required by lighting dev build)
$UV venv --python 3.10 .venv-torch

# 2. Torch stack (cu121, Pascal-compatible)
$UV pip install --python .venv-torch/bin/python \
    torch==2.3.1 torchvision==0.18.1 torchaudio==2.3.1 \
    --index-url https://download.pytorch.org/whl/cu121

# 3. Recipe deps (with workarounds for vintage code)
printf "setuptools<81\n" > /tmp/clip-build-constraints.txt
$UV pip install --python .venv-torch/bin/python \
    --build-constraint /tmp/clip-build-constraints.txt \
    "numpy==1.26.3" litgpt==0.4.3 snac==1.2.0 soundfile==0.12.1 \
    openai-whisper tokenizers==0.19.1 onnxruntime==1.19.0 \
    pydub==0.25.1 librosa==0.10.2.post1 fire \
    "git+https://github.com/mini-omni/CLIP.git"

# 3b. pkg_resources fix for lightning dev build
$UV pip install --python .venv-torch/bin/python "setuptools==69.5.1"

# 4. Clone + weights
git clone --depth 1 https://github.com/gpt-omni/mini-omni2
cd mini-omni2 && mkdir -p checkpoint
for f in lit_model.pth ViT-B-32.pt small.pt tokenizer.json tokenizer_config.json model_config.yaml; do
  curl -sL --retry 3 -o checkpoint/$f \
    "https://huggingface.co/gpt-omni/mini-omni2/resolve/main/$f"
done

# 5. Smoke test
cd mini-omni2
nice -n 10 ../.venv-torch/bin/python inference_vision.py
```