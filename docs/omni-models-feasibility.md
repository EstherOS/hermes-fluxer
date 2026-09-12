# Omni-models feasibility — candidates for the duplex slot on the GTX 1060 6GB

**Validity note:** This document describes what runs on ONE specific box (rootless podman container, NVIDIA GTX 1060 6GB Pascal, G4560 2C/4T). It provides **deployment guidance** for self-hosted model backends — not plugin behavior. The realtime/omni engine (`docs/spec-roles-profiles.md`) defines interface contracts; local model installation, binary configuration, and model fetching are **user behavior** documented here as reference for users of this hardware class. Readers with different hardware (or using cloud backends) should adjust accordingly.

Sources of truth for this doc: the GPU/model baseline is `docs/gpu-and-models.md`;
the candidate list being validated is `docs/model-recs-from-kairo.md`.

**Evidence tags** (same convention family as the other docs):

| Tag | Meaning |
| --- | --- |
| `(verified)` | Ran on this box tonight; raw logs under `docs/captures/omni-*`, artifacts under `models/omni/` |
| `(docs)` | Upstream docs / HF file listings / repo READMEs fetched tonight (HF API used for exact byte sizes) |
| `(inferred)` | Reasoning from the above — explicitly not directly observed |

Hard rules honored: no torch/framework installs (documented as "path B" in §6 instead),
no compiles, downloads capped — **2.50 GB** new (Qwen3-TTS + Qwen3-ASR GGUFs, §8),
all runs `nice -n 10` and bounded.

---

## 0. Bottom line (TL;DR)

1. **None of the three candidates runs on this box as shipped.** All three are
   PyTorch-first research releases with **no GGUF and no published quantization of
   any kind**; the "Q4 ~1–4 GB" sizes in `docs/model-recs-from-kairo.md` do **not
   correspond to any artifact that exists on HF** (true sizes: 11.8 GB BF16 /
   3.5 GB checkpoint set / 9.7 GB BF16, §1). Their local runtimes (torch,
   flash-attn, vLLM / litgpt) are either forbidden tonight, unsupported on
   Pascal (flash-attn needs Ampere+; vLLM needs sm_70+), or both.
2. **However, the box CAN now do all four senses in GGUF runtime — three of them
   newly verified tonight:**
   - text → llama.cpp Vulkan (was good), **image → SmolVLM2** (was good),
   - **audio-in → Qwen3-ASR-0.6B GGUF via llama.cpp, GPU** *(new, verified)* + whisper.cpp CPU (was good),
   - **audio-out → Qwen3-TTS-1.7B GGUF via `llama-tts`, GPU, with voice cloning** *(new, verified)* + piper (was good),
   - **video → native `--video` in `llama-mtmd-cli`** *(new, verified)* + frame captioning (was good).
3. **Single-model "duplex" (audio+vision in, text+speech out, one model) is not
   attainable on 6 GB tonight.** The closest open-weights candidates need 12 GB+
   (MiniCPM-o 4.5 — needs its own fork `llama.cpp-omni`, upstream says ≥12 GB GPU)
   or simply have no weights you can run (MiMo-Audio-7B — mainline llama.cpp
   *has the code*, no GGUF released yet).
4. **Recommended duplex plan:** tonight = orchestrated **cascade** with the verified
   GGUF components (ASR → llama.cpp LLM → TTS; SmolVLM2 handles image/video frames).
   Next purchase of bandwidth = **Qwen2.5-Omni-3B GGUF** (3.4 GB, single model,
   audio+image+video→text; pair with Qwen3-TTS for speech out) — exact commands in §5.
5. The truly-duplex three candidates map to hardware we don't have; **Mini-Omni2 is
   the only one with a plausible future on this box** (0.5 B, ~2–3 GB VRAM est.,
   torch-only), and even that is a "later wave, budget ~6 GB downloads" item, not a
   tonight win.

---

## 1. Candidate verdicts

### 1.1 InteractiveOmni-4B — **not runnable tonight; not soon on 6 GB**

| Fact | Value |
| --- | --- |
| Upstream | HF [`sensenova/InteractiveOmni-4B`](https://huggingface.co/sensenova/InteractiveOmni-4B) · code [OpenSenseNova/InteractiveOmni](https://github.com/OpenSenseNova/InteractiveOmni) (SenseTime) · paper [arXiv:2510.13747](https://arxiv.org/abs/2510.13747) `(docs)` |
| License | MIT (model card tag) `(docs)` |
| Architecture | InternViT vision + Whisper audio encoder + **Qwen3-4B** LLM + **CosyVoice2-based speech decoder** (speech-token LM + token2wav) `(docs)` |
| Weights on disk | **11.83 GB BF16** (3 × safetensors: 4.74 + 4.76 + 2.33 GB) + small aux (`campplus.onnx` speaker encoder 27 MB, code) `(docs)` |
| Quantized / GGUF release | **None anywhere.** HF search "InteractiveOmni" → only the two official repos (4B, 8B). No GGUF, no GPTQ/AWQ/ONNX-model beyond the 27 MB speaker encoder `(docs)` |
| Runtime | `transformers >= 4.51` + `trust_remote_code` (custom modules: `modeling_interactiveomni`, `modeling_voicelm`, `modeling_flow`, `modeling_hifigan`, whisper, intern_vit). `requirements.txt`: torch, torchaudio, torchvision, **flash_attn**, decord, onnxruntime, einops, diffusers, omegaconf, scipy, timm, librosa. README: *"Please use transformers>=4.51.0 and FlashAttention2 to ensure the model works normally."* `(docs)` |
| Blockers on this box | (a) No quant ⇒ BF16 4B+encoders ≈ **10–12 GB VRAM minimum**; 6 GB can't hold it. (b) Upstream demands **FlashAttention2**, which requires **Ampere+ (sm_80)**; our Pascal sm_61 is unsupported `(docs/inferred)`. (c) torch + flash-attn + decord etc. ≈ 3–5 GB installs, explicitly out of tonight's rule set. |
| Claimed "~3.0–4.0 GB Q4" | Does not exist. Closest real artifact would need a community 4-bit quant that has not been published `(docs)`. |
| llama.cpp support | None — b10903 binaries contain zero `interactiveomni`/`cosyvoice` strings `(verified: strings on libllama/libmtmd)` |

**Verdict: `not yet`.** A GPU box with ≥12–16 GB (or an expert 4-bit conversion project)
is required; it is not a path-B "just install torch" item. See §6 for the honest recipe.

### 1.2 Mini-Omni2 — **not tonight; the only candidate with a plausible later path on this box**

| Fact | Value |
| --- | --- |
| Upstream | HF [`gpt-omni/mini-omni2`](https://huggingface.co/gpt-omni/mini-omni2) · code [gpt-omni/mini-omni2](https://github.com/gpt-omni/mini-omni2) · paper [arXiv:2410.11190](https://arxiv.org/abs/2410.11190) `(docs)` |
| License | MIT `(docs)` |
| Architecture | ~0.5 B LLM (litgpt format) + CLIP ViT-B/32 vision + Whisper audio encoder + **SNAC codec** for speech out (24 kHz); "end-to-end voice conversations… with interruption mechanism" `(docs)` |
| Weights on disk | **3.48 GB total**: `lit_model.pth` 2.68 GB + `ViT-B-32.pt` 338 MB + `small.pt` 461 MB (+ tokenizer) `(docs)` |
| Quantized / GGUF release | **None.** HF search "Mini-Omni" → upstream + third-party mirrors only (no GGUF). b10903 binaries: zero `miniomni` strings `(verified)` |
| Runtime | torch==2.3.1 / torchvision / torchaudio, **litgpt==0.4.3**, snac==1.2.0, soundfile, openai-whisper, tokenizers==0.19.1, onnxruntime, pydub, librosa, flask/fire, a custom CLIP fork; `server.py` + `inference_vision.py`; streamlit/gradio demos need PyAudio and a browser `(docs)` |
| Blockers on this box | (a) torch stack install (~2.2–2.9 GB wheels alone) — forbidden tonight; (b) no quant exists, so no llama.cpp path; (c) partially the "server" flow assumes PyAudio/browser for full duplex demo. |
| Feasibility estimate if installed | VRAM est. **~2–3 GB** (0.5 B fp16 ≈ 1 GB + encoders + SNAC) → should fit 6 GB; Pascal sm_61 is fine for torch 2.3.1 cu121 wheels `(inferred)`. Speed unknown; CPU is weak (G4560, 2C/4T) but the model is tiny. |
| Claimed "~1.5 GB Q4" | Does not exist — no quantized checkpoint of any kind `(docs)`. |

**Verdict: `not yet (plausible path B)`.** Budget: ~6 GB downloads, a new venv, an
afternoon. If the fluxer plugin ever wants a *single-model* cheap duplex toy, this is
the one to try first. Exact recipe in §6.

### 1.3 Aero Realtime-4B — **not runnable on this GPU class at all**

| Fact | Value |
| --- | --- |
| Upstream | HF [`kcz358/aero-realtime-4B`](https://huggingface.co/kcz358/aero-realtime-4B) · code [kcz358/aero-realtime](https://github.com/kcz358/aero-realtime) · serving stack **vLLM-Omni** · paper [arXiv:2608.08469](https://arxiv.org/abs/2608.08469) `(docs)` |
| License | MIT `(docs)` |
| Architecture | ~4.86 B params; initialized from **Qwen3-VL-4B** (vision tower + LM) + **Qwen3-Omni audio tower**; 80 ms audio-slot grid; single LM head predicts lexical token **or silence token** — duplex input/output on one causal clock; output capped at ~12.5 tokens/s `(docs)` |
| Weights on disk | **9.71 GB BF16** single `model.safetensors` `(docs)` |
| Quantized / GGUF release | **None.** Single repo; no GGUF/GPTQ. b10903 binaries: zero `aero` strings `(verified)` |
| Runtime | custom transformers code + **vLLM-Omni** deploy config (`vllm_omni/deploy/aero_realtime.yaml`); `examples/offline/demo.py`, `examples/online/server.py` (WebSocket `/v1/aero/realtime`); "resumable" KV-cache delta inference. Training via LMMs-Engine. `(docs)` |
| Blockers on this box | (a) **vLLM requires compute capability ≥ 7.0 (Volta+)** — GTX 10-series (sm_61, Pascal) is not supported `(docs/inferred)`; (b) BF16 ~10 GB + audio/video encoders ⇒ 12 GB+ VRAM; (c) no GGUF ⇒ no fallback runtime. |
| Audio out note | The repo's model emits **lexical/silence tokens**; the project website shows "chunked audio waveforms" but the released repo/code shows no vocoder/token2wav component — treat **audio-waveform output as demo-side, unverified** `(inferred)` |
| Claimed "~3.0–3.5 GB Q4_K_M" | Does not exist; and the "5B, over ≤4B" note in model-recs is right — it's ~4.9 B `(docs)` |

**Verdict: `not yet + not on Pascal`.** Reference architecture to watch; would need a
rented/modern GPU (sm_70+, ≥12–16 GB) plus the vLLM-Omni stack.

---

## 2. Four-senses map — what actually runs on this box tonight

Everything below is `(verified)` on the GTX 1060 6GB unless marked otherwise. Commands
are copy-paste; `. gpu/gpu-env.sh` is required in every new shell.

| Sense | What runs now | How | Evidence |
| --- | --- | --- | --- |
| **text → text** | any GGUF LLM ≤ ~5 GB (e.g. Qwen2.5-3B Q4_K_M) | `gpu/tools/llama-b10903/llama-cli -m … -c 4096` (Vulkan, full offload) | prior wave (`docs/gpu-and-models.md`); b10903 arch list widened further (§3) |
| **image → text** | SmolVLM2-256M-Video (installed) | `llama-mtmd-cli -m models/smolvlm2-256m.gguf --mmproj models/mmproj-smolvlm2.gguf --image X.jpg -p "Describe."` | prior wave + re-verified tonight (`docs/captures/omni-video-smoke.log` run) |
| **audio → text (in)** | **Qwen3-ASR-0.6B GGUF (new tonight, GPU)** and whisper.cpp tiny.en (CPU) | `scripts/omni_asr_smoke.sh [audio.wav]` · or `whisper-cli -m models/ggml-tiny.en.bin -f a.wav -t 4` | **new**: exact transcript of `jfk.wav`; 1527 MiB VRAM peak, 81% GPU util; server HTTP path also verified (§3.3) |
| **text → speech (out)** | **Qwen3-TTS-1.7B GGUF (new tonight, GPU, voice cloning)** and piper (CPU) | `scripts/omni_tts_smoke.sh "text" out.wav speaker.wav` · or piper one-liner | **new**: 6.40 s speech in 11.82 s; whisper.cpp re-transcribed it correctly; ~2.5 GB VRAM peak |
| **video → text** | SmolVLM2-256M-Video, two ways: **native `--video`** (new tonight) or frame-by-frame captioning | native: `llama-mtmd-cli … --video clip.mp4 --video-fps 2` · frames: `scripts/caption_video.py clip.mp4` (JSONL w/ timestamps) | **new**: 2 s test video → correct caption; native path needs `-c 8192` for that clip (§3.4) |

Exact verified commands (abridged — full flags in the scripts):

```bash
. gpu/gpu-env.sh
# audio-in (new)
scripts/omni_asr_smoke.sh models/jfk.wav
# → "language English<asr_text>And so, my fellow Americans, ask not what your country can do for you; ask what you can do for your country."

# audio-out (new; 24 kHz WAV out, jfk.wav used as the voice prompt)
scripts/omni_tts_smoke.sh "Hello from Fluxer. Local speech on the GTX 1060." models/omni/out/tts-smoke.wav models/jfk.wav
# → "generated 80 frames, 307244 bytes of WAV audio (24000 Hz)"; "output audio = 6.40s"

# video (new: native video flag)
ffmpeg -y -loop 1 -i models/test-dog.jpg -t 2 -r 2 -vf scale=320:-2 -pix_fmt yuv420p /tmp/clip.mp4
gpu/tools/llama-b10903/llama-mtmd-cli -m models/smolvlm2-256m.gguf --mmproj models/mmproj-smolvlm2.gguf \
  --video /tmp/clip.mp4 --video-fps 2 -p "Describe what you see." -n 48 -ub 64 -b 128 -c 8192
```

Latency reality check (measured, not marketing): ASR of an 11 s clip ≈ 4 s end-to-end
via HTTP (45 tok/s prompt eval, 121 tok/s gen); TTS ≈ 0.5–1.0× realtime depending on
length; SmolVLM2 caption ≈ 6–10 s per image/frame incl. load. Expect **2–5 s
turnaround for a short voice turn** on the cascade — usable for async chat, not for
natural interrupt-driven duplex.

### What "duplex" means here tonight

The product slot ("single model, text/audio/image/video in → text/audio out, full-duplex")
cannot be filled by one model on 6 GB (see §1, §4). What we can field now is a
**half-duplex cascade** orchestrated by the fluxer plugin (STT → LLM → TTS), which
covers every sense with GGUF parts that are already verified. The full-duplex
behavior ideally belongs to a future model on bigger hardware (§5 phase 2).

---

## 3. llama.cpp b10903 deep-dive (what this build can actually do)

### 3.1 Input support

libmtmd in this build handles **image, audio and video input**, and the binary set
includes `llama-mtmd-cli`, `llama-mtmd-debug`, `llama-tts`, `llama-server`.
Audio-input families present in `libmtmd.so` strings `(verified)`:

```
whisper-enc · qwen3a (Qwen3-ASR) · qwen2a (Qwen2-Audio) · ultravox · voxtral
granite_speech (IBM Granite Speech) · mimo_audio (Xiaomi MiMo-Audio) · gemma4a / gemma4ua (Gemma 4 audio)
```

Pre-quantized audio/omni GGUFs that upstream documents as ready-to-use `(docs)`,
with exact sizes from the HF API `(docs)`:

| Model (ggml-org) | Files | Total | Capability |
| --- | --- | --- | --- |
| `ultravox-v0_5-llama-3_2-1b-GGUF` | 0.77 GB + mmproj 1.31 GB | 2.08 GB | audio → text |
| `Voxtral-Mini-3B-2507-GGUF` | 2.36 GB + mmproj 0.68 GB | 3.04 GB | audio → text |
| `Qwen3-ASR-0.6B-GGUF` / `-1.7B-GGUF` | 0.77 + 0.20 / 2.06 + 0.34 GB | audio → text |
| `Qwen2.5-Omni-3B-GGUF` | 2.01 GB + mmproj 1.47 GB | **3.47 GB** | **audio + vision → text** |
| `Qwen2.5-Omni-7B-GGUF` | 4.47 GB + mmproj 1.48 GB | 5.94 GB | same, bigger (borderline on 6 GB) |
| `Qwen3-Omni-30B-A3B-Instruct/Thinking-GGUF` | ~18 GB+ est. | way over | audio + vision → text |
| `gemma-4-E2B-it-GGUF` (`E4B`) | 2.71 (4.38) + mmproj 0.53 + mtp 0.06 | **3.31 GB** (E2B) | **audio + vision → text** |

(Ultravox-8B, Voxtral-24B, Qwen2-Audio: Qwen2-Audio explicitly has *no* official
GGUF — upstream says results were poor; third-party 7B GGUFs exist but are not part
of the supported set `(docs)`.)

### 3.2 Output support — `llama-tts` (audio generation)

This build ships a model-agnostic **TTS tool** (`llama-tts`, PR #26254 family) driven
by libmtmd "gen" pipelines `(verified: strings + run)`:

| TTS model | GGUF | Size | Notes |
| --- | --- | --- | --- |
| **Qwen3-TTS-12Hz-1.7B-Base** (ggml-org) | `Q4_K_M` + `mmproj-Q8_0` | 1.41 GB | **verified tonight**; `--tts-lang` (en/zh/de/it/pt/es/ja/ko/fr/ru), `--tts-speaker-file` = voice cloning |
| Qwen3-TTS 0.6B/1.7B "talker" variants (Serveurperso) | Q4_K_M | 0.60/1.16 GB | community GGUFs, same family — **not tested** |
| PocketTTS (Kyutai, via cstr + others) | `q4_k`/`q8_0` per language | 0.06–0.21 GB | tiny voice-clone TTS; `pockettts` strings present in build; needs `--tts-speaker-file` (required); **not tested** |
| OuteTTS (legacy) | OuteAI GGUFs exist (0.5–1B) | ~0.3–0.8 GB | the `llama-tts` tool is the *former* OuteTTS demo, now model-agnostic; OuteTTS GGUFs not retested; prefer Qwen3-TTS |

`mimo_audio` also has a full **gen pipeline incl. `code2wav`** in libmtmd — i.e. mainline
llama.cpp is *code-ready* for a speech-in/speech-out model (MiMo-Audio), but **no model
GGUF has been published** (only `cstr/mimo-audio-tokenizer-GGUF`, a 1.24 GB audio
tokenizer; weights are 15.3 GB BF16 safetensors) `(docs)`.

No support found for: CosyVoice2, Kokoro (`cstr/kokoro-82m-GGUF` exists but no
`kokoro` strings in this build), InteractiveOmni, Mini-Omni2, Aero `(verified: strings)`.

### 3.3 Verified smoke tests (tonight, all on the 1060)

| Test | Result | Log |
| --- | --- | --- |
| Qwen3-TTS 1.7B Q4 → speech | 80 frames → 6.40 s WAV (24 kHz) in 11.82 s; whisper.cpp re-transcribed output exactly; peak VRAM ~2.46 GB | `docs/captures/omni-tts-smoke.log`, `omni-tts-vram.log`, `models/omni/out/tts-smoke.wav` |
| Qwen3-ASR 0.6B Q8 ← `jfk.wav` | exact transcript; 1527 MiB VRAM, 81% GPU util | `docs/captures/omni-asr-smoke.log` |
| **llama-server HTTP audio** | OpenAI-compatible `input_audio` (base64) → correct transcript; `capabilities:["completion","multimodal"]`; 4.03 s total for 11 s audio | `docs/captures/omni-asr-server-evidence.md` |
| native video input | SmolVLM2 2 s clip → "In the image, a black puppy…"; 105 media chunks encoded at ~160 ms each | `docs/captures/omni-video-smoke.log` |

**Integration surface for the plugin:** `llama-server` is the HTTP path — multimodal is
advertised on `/v1/models` and audio rides in `/v1/chat/completions` as
`{"type":"input_audio","input_audio":{"data":"<base64>|url|path"}}` (mp3/wav/flac; format
auto-detected) `(docs)` + `(verified)` for the audio case. **TTS has no server endpoint** —
`llama-tts` is a CLI tool; the plugin should shell out (script provided) or keep piper.

### 3.4 Gotchas learned tonight (write these down)

- **Keep `-c` small for llama-tts/llama-mtmd-cli on 6 GB.** With the model's default
  context, graph reservation tried a **single ~917 MB Vulkan buffer** and failed
  (`ErrorOutOfDeviceMemory`) *while 6.0 GB was free* — retry with `-c 1024`
  (and `-ub 64 -b 128`) works reliably. Verified both `-ub 512` and `-ub 64` are fine
  once `-c` is capped; the ctx size was the trigger. If a future model needs big ctx,
  expect to fight this again — next dials: `-ub 64`, `--no-mmproj-offload`, partial `-ngl`.
- Video needs context proportional to frames: 2 s @ 2 fps produced 105 media chunks
  and needed `-c 8192` (at 2048 it died with *"failed to find a memory slot"*).
- Only 2 threads initialized (G4560, 2C/4T) — CPU-side prompt processing is the
  bottleneck for long prompts; keep prompts short.
- `llama-mtmd-cli` writes the reply to **stdout**, logs to **stderr** (parsed
  accordingly in `scripts/caption_video.py`).

---

## 4. Wider GGUF audio/omni landscape (what exists at all, 2026-09-11)

Sorted by usefulness for *this* box. Sizes from HF API `(docs)`; support column checked
against b10903 strings/docs `(verified)`/`(docs)`.

| Model | Quant sizes | Capability | b10903 runnable? | Fit on 6 GB? | License |
| --- | --- | --- | --- | --- | --- |
| **Qwen2.5-Omni-3B** | Q4_K_M 2.01 GB + mmproj 1.47 GB | audio+image+video(frames) → text | **yes** (documented) | yes, tight (~3.5 GB) | Apache-2.0 (upstream) |
| **Gemma 4 E2B** | Q4_0 2.71 GB + mmproj 0.53 GB | audio+vision → text | **yes** (documented) | yes (~3.3 GB) | Apache-2.0 |
| Qwen3-ASR 0.6B/1.7B | 0.77 + 0.20 (0.6B); 2.06 + 0.34 (1.7B) | audio → text | **yes — verified** | yes | Apache-2.0 |
| Voxtral-Mini-3B-2507 | 2.36 + 0.68 | audio → text | yes | yes (~3.0 GB) | Apache-2.0 |
| Ultravox 1B | 0.77 + 1.31 | audio → text | yes | yes | MIT |
| IBM Granite Speech 4.1-2b | 1.09 + mmproj 1.11 | audio → text/translate | yes (`granite_speech` present) | yes (~2.2 GB) | Apache-2.0 |
| Qwen3-TTS 1.7B Base | 0.99 + 0.43 | **text → speech, voice clone** | **yes — verified** | yes | Apache-2.0 (upstream) |
| PocketTTS | 0.06–0.21 | text → speech, voice clone | yes (`pockettts` present) | trivially | CC-BY-4.0 |
| Qwen2.5-Omni-7B / Voxtral-24B / Qwen3-Omni-30B-A3B | 4.47→18+ GB | omni understanding | yes | **no** (too big) | various |
| MiniCPM-o 2.6 (7.6B) | Q4_K_M 4.46 + mmproj 1.00 | vision+audio (speech historically fork-only) | **partial / untested**: upstream doc covers *image mode* only | borderline (5.5 GB) | Apache-2.0 |
| **MiniCPM-o 4.5 (9B)** | Q4_K_M 4.79 (Q4_K_S 4.58) + aux ≈1.6 → full set **≈8.2 GB** | **full duplex**: video+audio+text in → text+speech out | **NO mainline** — needs fork [`tc-mb/llama.cpp-omni`](https://github.com/tc-mb/llama.cpp-omni) (C++); upstream: **≥12 GB GPU** for half- or full-duplex | no | Apache-2.0 |
| MiMo-Audio-7B-Instruct | **no GGUF yet** (15.3 GB BF16) | speech in → speech out (duplex-ish) | code present (`mimo_audio` + `code2wav`), no weights | would be tight (~4.5–5 GB Q4) if released | MIT |
| **Mini-Omni2 (GGUF via CrispASR)** | Q4_K 0.99 GB, F16 1.47 GB + snac-24khz GGUF 377 MB | **speech in → speech out, ASR, TTS**, full duplex, interruption | **NO (b10903)** → CrispASR v0.7.2+ (C++ ggml, CUDA/Vulkan, ~106 backends) — **untested on Pascal sm_61** | yes, comfortable (~1.5–2.0 GB with codec) | MIT (upstream) |
| Qwen2-Audio-7B | third-party GGUFs only | audio → text | upstream declined official quant | not officially supported | Apache-2.0 |
| Kokoro-82M | 0.08–0.3 | text → speech | **no** (no `kokoro` strings) — other runtimes | n/a | various |

Takeaway: the GGUF ecosystem **can see and hear** on this box, and can **speak**;
what it cannot yet do in one process on ≤6 GB is the *combined* "sees, hears, speaks,
full-duplex" model. The nearest open-weights single models for that are MiniCPM-o 4.5
(needs 12 GB+ and a fork) and MiMo-Audio-7B (needs a GGUF that doesn't exist yet).

---

## 5. Recommended duplex-slot plan

### Phase 0 — tonight/now: GGUF cascade (all verified, all local)

`fluxer plugin (Hermes) orchestrates:` **whisper.cpp or Qwen3-ASR** (audio-in) →
**llama.cpp LLM** (any GGUF ≤5 GB; text brain) → **Qwen3-TTS or piper** (speech-out);
**SmolVLM2** for image and video captions; ffmpeg for frame extraction. Everything on
the 1060, no torch, no cloud. "Duplex" = half-duplex turns with the plugin managing
interruption (drop the TTS, restart the loop). Cost: 0 GB new downloads — all
components are installed or were fetched tonight.

- scripts: `scripts/omni_asr_smoke.sh`, `scripts/omni_tts_smoke.sh`, `scripts/caption_video.py`
- HTTP integration: run `llama-server` with an audio model → `input_audio` JSON (verified).

### Phase 1 — next download (≈3.4 GB): one model for 3 senses-in

**`ggml-org/Qwen2.5-Omni-3B-GGUF`** (audio + image + video-frames *in*, text out) —
fits 6 GB, is upstream-documented for this exact build, and removes the need to pick
between whisper and SmolVLM2 per message. Pair with Qwen3-TTS (already downloaded)
for the out-bound speech. Exact steps when bandwidth allows:

```bash
. gpu/gpu-env.sh
mkdir -p models/omni/qwen2.5-omni-3b
BASE=https://huggingface.co/ggml-org/Qwen2.5-Omni-3B-GGUF/resolve/main
curl -L -o models/omni/qwen2.5-omni-3b/Qwen2.5-Omni-3B-Q4_K_M.gguf          "$BASE/Qwen2.5-Omni-3B-Q4_K_M.gguf"      # 2007 MB
curl -L -o models/omni/qwen2.5-omni-3b/mmproj-Qwen2.5-Omni-3B-Q8_0.gguf     "$BASE/mmproj-Qwen2.5-Omni-3B-Q8_0.gguf" # 1467 MB

# smoke (keep ctx modest on 6 GB):
gpu/tools/llama-b10903/llama-mtmd-cli -m models/omni/qwen2.5-omni-3b/Qwen2.5-Omni-3B-Q4_K_M.gguf \
  --mmproj models/omni/qwen2.5-omni-3b/mmproj-Qwen2.5-Omni-3B-Q8_0.gguf \
  --audio models/jfk.wav -p "Describe this audio." -n 128 -ub 64 -b 128 -c 2048
```

Fallbacks if VRAM is tight: `--no-mmproj-offload` (encoder on CPU) or `-ngl N`
partial offload. Alternative same-class pick: **Gemma 4 E2B** (2.71 + 0.53 GB) — newer
arch, also audio+vision-in per upstream; not yet tested in the wild here.

### Phase 2 — the "real" duplex slot (needs bigger hardware, or patience)

- **MiniCPM-o 4.5 via `llama.cpp-omni`** — the honest match for the user's dream model:
  9 B, video+audio in / text+speech out, full-duplex live streaming. Reality: fork-only
  (no Linux prebuilt; Docker or source build), upstream requires **≥12 GB VRAM**, and the
  full GGUF set is ≈8.2 GB on disk. Not this box. (On an RTX 3060-12G/4070-class card it
  becomes a weekend project.)
- **MiMo-Audio-7B** — mainline llama.cpp already has its pipeline; **watch HF for a GGUF**
  (none as of tonight). If a Q4 appears (~4.5 GB), it's speech-in → speech-out and the
  best single-model duplex that could *maybe* squeeze into 6 GB with small ctx.
- `docs/model-recs-from-kairo.md` should get a one-line correction: the model *ideas* were sound, but none of the three shipped a quantized artifact (sizes there were off by 3–4×, §1).

---

## 6. Path B — running the actual candidates (documented, not attempted)

Per hard rules: **not attempted tonight** — no torch/heavy installs. Exact recipes so a
future wave can pick this up. Shared prep:

```bash
UV=/home/agent/.hermes/bin/uv
$UV venv /home/agent/workspace/fluxer/.venv-torch
```

### 6.1 Mini-Omni2 (the cheapest path B; ~6 GB downloads; likely works on 6 GB)

```bash
$UV pip install --python .venv-torch/bin/python \
    torch==2.3.1 torchvision==0.18.1 torchaudio==2.3.1 \
    --index-url https://download.pytorch.org/whl/cu121          # ~2.2–2.9 GB
$UV pip install --python .venv-torch/bin/python \
    litgpt==0.4.3 snac==1.2.0 soundfile openai-whisper tokenizers==0.19.1 \
    onnxruntime==1.19.0 pydub librosa fire "git+https://github.com/mini-omni/CLIP.git"
git clone https://github.com/gpt-omni/mini-omni2 && cd mini-omni2
# weights (3.48 GB): download the HF repo files (lit_model.pth, ViT-B-32.pt, small.pt)
# then: python3 inference_vision.py   (preset samples)  /  server.py for the API mode
```

Caveats: torch 2.3.1 cu121 covers Pascal sm_61 fine; the upstream stack pins old
versions (python 3.10 suggested — the venv above is 3.11; may need `uv venv --python 3.10`);
no quant ⇒ no llama.cpp path; est. VRAM ~2–3 GB `(inferred)`.

### 6.2 InteractiveOmni-4B (~15 GB downloads; blocked by Pascal + VRAM, don't)

```bash
$UV pip install --python .venv-torch/bin/python torch torchaudio torchvision \
    --index-url https://download.pytorch.org/whl/cu124          # ~2.5 GB+
$UV pip install --python .venv-torch/bin/python "transformers>=4.51" decord librosa \
    onnxruntime einops diffusers omegaconf scipy timm Pillow numpy
# flash_attn: DO NOT attempt on Pascal — requires Ampere+ (sm_80); upstream
#   marks FA2 as required for correct behavior, so expect quality issues/fallback.
# weights: 11.8 GB BF16 safetensors (download ≈12 GB) — needs ≥12 GB VRAM to run BF16.
# On 6 GB the only theoretical path is bitsandbytes 4-bit + offload gymnastics over a
#   CosyVoice2 stack — not a documented path, likely to fight for days.
```

### 6.3 Aero Realtime-4B (not feasible on this GPU class — rent or skip)

```bash
# serving requires vLLM-Omni (vllm + torch). vLLM supports compute capability >= 7.0;
# GTX 1060 = sm_61 → unsupported. Weights 9.7 GB BF16 ⇒ ≥12 GB VRAM even 4-bit-adjacent.
# Reference commands (for a future A10/3090/A6000-class box):
python examples/offline/demo.py --model kcz358/aero-realtime-4B \
    --deploy-config /path/to/vllm-omni/vllm_omni/deploy/aero_realtime.yaml
```

---

## 7. Unknowns / open questions

- **Qwen2.5-Omni-3B real VRAM** with ctx on 6 GB — untested (download deferred, §5).
- **MiniCPM-o 4.5 on a 12 GB card** — untested here by definition; also whether
  `llama.cpp-omni` has Linux prebuilts is unclear (publicly: Windows/macOS installers;
  Linux appears to be Docker/source).
- **MiMo-Audio GGUF timeline** — mainline code exists but may be immature; check upstream
  PRs before trusting it. Also whether a 7B Q4 + encoders can truly fit 6 GB (borderline).
- **Mini-Omni2 installability** — dependency pins (vintage 2024) on python 3.11+ are
  unverified; may need python 3.10.
- **Quality assessments** — we verified *mechanics* (correct transcripts, plausible
  speech) not quality: TTS voice-clone similarity vs the reference speaker was not
  evaluated by ear; ASR tested on clean studio audio only (no noisy chat audio);
  SmolVLM2 captions are generic and occasionally say "In the image" for video.
- **The 917 MB allocation failure** trigger was narrowed (ctx size; `-c 1024` fix) but not
  exhaustively bisected; if a future model needs 8k+ ctx, expect to trade off
  `-ub`/`--no-mmproj-offload`/partial offload.
- **llama-tts multilingual** — `--tts-lang` lists 10 languages per README; only `en`
  tested.
- **InteractiveOmni 4-bit convertibility** — nobody has published a quant; conversion is
  a project, not a download.

---

## 8. Appendix

### 8.1 Files created/changed tonight (all under `/home/agent/workspace/fluxer/`)

| Path | What |
| --- | --- |
| `docs/omni-models-feasibility.md` | **this document** |
| `scripts/omni_asr_smoke.sh` | audio-in smoke test (Qwen3-ASR GGUF, llama-mtmd-cli) |
| `scripts/omni_tts_smoke.sh` | audio-out smoke test (Qwen3-TTS GGUF, llama-tts, voice clone) |
| `scripts/caption_video.py` | frame-by-frame video captioner → JSONL (SmolVLM2 + ffmpeg) |
| `docs/captures/omni-tts-smoke.log` | raw llama-tts run log (verified evidence) |
| `docs/captures/omni-tts-vram.log` | llama-tts run + VRAM sampling run |
| `docs/captures/omni-asr-smoke.log` | raw llama-mtmd-cli ASR log |
| `docs/captures/omni-asr-server-evidence.md` | llama-server HTTP audio test excerpt + recipe |
| `docs/captures/omni-video-smoke.log` | raw native `--video` run log |
| `models/omni/qwen3-tts-1.7b/` | Qwen3-TTS Q4_K_M + mmproj (1.41 GB) |
| `models/omni/qwen3-asr-0.6b/` | Qwen3-ASR-0.6B Q8_0 + mmproj (0.97 GB) |
| `models/omni/out/` | tts-smoke.wav, tts-script-test.wav, dog-2s.mp4, dog-2s-captions.jsonl, frames-* |

### 8.2 Download accounting (cap: ~2.5 GB → used 2.50 GB decimal)

| File | Bytes |
| --- | --- |
| Qwen3-TTS-12Hz-1.7B-Base-Q4_K_M.gguf | 1,035,965,280 |
| mmproj-Qwen3-TTS-12Hz-1.7B-Base-Q8_0.gguf | 446,422,912 |
| Qwen3-ASR-0.6B-Q8_0.gguf | 804,749,248 |
| mmproj-Qwen3-ASR-0.6B-Q8_0.gguf | 214,392,480 |
| **Total** | **2,501,529,920 (2.33 GiB)** |

Disk before ≈19 GB free → after ≈17 GB free (`df -h /home/agent/workspace`).

### 8.3 One-screen cheat sheet

```bash
cd /home/agent/workspace/fluxer && . gpu/gpu-env.sh
scripts/omni_asr_smoke.sh models/jfk.wav                      # hear (GPU)
scripts/omni_tts_smoke.sh "text to speak" out.wav ref.wav     # speak (GPU, voice clone)
scripts/caption_video.py clip.mp4 --fps 0.5 --max-frames 8    # see video (GPU)
gpu/tools/llama-b10903/llama-cli  -m <llm.gguf> -c 4096 -st   # think (GPU)
gpu/tools/whisper-bin-ubuntu-x64/whisper-cli -m models/ggml-tiny.en.bin -f a.wav -t 4  # hear (CPU fallback)
echo "hi" | gpu/tools/piper/piper --model models/en_US-lessac-medium.onnx --output_file out.wav  # speak (CPU fallback)
```
