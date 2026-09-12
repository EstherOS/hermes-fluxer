# GPU & Models — Vulkan on the container's NVIDIA GPU

**Status (2026-09-11 ~07:55 UTC): VULKAN FIXED — no root, no host changes needed.**
Verified working: `vulkaninfo`, `llama.cpp` (Vulkan build), SmolVLM2 image captioning, whisper.cpp STT (CPU), piper TTS (CPU). CUDA also works (`cuInit(0) → 0`).

> **10-second check:** `. /home/agent/workspace/fluxer-local/gpu/gpu-env.sh && vulkaninfo --summary | grep deviceName`
> → `deviceName = NVIDIA GeForce GTX 1060 6GB`  (also: `/home/agent/workspace/fluxer-local/gpu/verify-gpu.sh`)

---

## 1. Hardware reality check (verified facts)

| Item | Value |
|---|---|
| GPU | **NVIDIA GeForce GTX 1060 6GB** (PCI `0000:01:00.0`, GV106, deviceID `0x1c03`) — ⚠️ *task said "GTX 1650"; the actual card is a 1060 6GB (nvidia-smi, /proc/driver/nvidia, vulkaninfo all agree). Plan around Pascal-class performance.* |
| VRAM | 6144 MiB (nvidia-smi) / 6390 MiB usable (llama.cpp); **~6300 MiB free when idle**. Budget for model+ctx ≈ 5.5–6.0 GB. |
| Driver | 550.127.05 (kernel module + userspace match), nvidia-smi works, CUDA 12.4 usable: `cuInit(0)=0`, `cuDeviceGetName → NVIDIA GeForce GTX 1060 6GB`. |
| Devices | `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm(-tools)` (mode 666), `/dev/dri/card0`, `renderD128` — all readable by uid 1000. |
| Vulkan | Loader 1.4.309; `vulkaninfo` present. NVIDIA driver Vulkan API **1.3.277**. |
| CPU | Intel Pentium **G4560 (2C/4T @3.5 GHz)** — weak; GPU offload matters a lot for LLM. 15 GiB RAM, 6 GiB swap. |
| Constraints | rootless podman, no root, `/usr` read-only, no pip (use `uv`), writes only under `/home/agent/workspace/fluxer-local/`. |

---

## 2. What was broken & why (diagnosis chain)

Symptom (before fix): `vulkaninfo` → `loader_scanned_icd_add: Could not get 'vkCreateInstance' via vk_icdGetInstanceProcAddr for ICD libGLX_nvidia.so.0` → `Found no drivers!` → `ERROR_INCOMPATIBLE_DRIVER`.

Root cause chain, proven step by step:

1. `/etc/vulkan/icd.d/nvidia_icd.json` is **valid** (points at `libGLX_nvidia.so.0`, api 1.3.277); the lib **dlopens fine** and exports `vk_icd*` symbols.
2. Direct ctypes probe (`gpu/probe_icd.py`): the ICD's **internal init fails** — `vk_icdNegotiateLoaderICDInterfaceVersion → -3` (VK_ERROR_INITIALIZATION_FAILED) and every `vk_icdGetInstanceProcAddr(NULL, …)` → NULL.
3. `LD_DEBUG=libs` run (`gpu/vulkan_lddebug.log`): during ICD init the driver keeps trying to `dlopen("libEGL.so.1")` — **which does not exist in this container**. Its absence aborts the ICD init.
4. This is the **known NVIDIA container issue** (nvidia-container-toolkit #191, #1472, #1732, #1952): the fix on normal systems is "install `libegl1` in the container". We can't install packages into `/usr`, so we fixed it in user space instead (below). Same class of issue: NVIDIA docs say *"in environments where X11 client libraries are not available, `libEGL_nvidia.so.0` should be used [as the Vulkan ICD]"*.

Dead end tried first (for the record): `LD_DEBUG` also flagged `undefined symbol: __malloc_hook/__realloc_hook/__free_hook/__memalign_hook` in `libnvidia-glcore` (removed in glibc ≥2.34; container has 2.41). A shim providing those (`gpu/nvcompat_shim.c` → `gpu/libnvcompat.so`) did **not** fix anything — red herring, kept for reference only.

---

## 3. The fix (working recipe)

Everything lives in `/home/agent/workspace/fluxer-local/gpu/`. **Source this in every shell before running GPU apps:**

```bash
. /home/agent/workspace/fluxer-local/gpu/gpu-env.sh
```

What it exports (two independent layers — each alone is enough; together they're belt-and-braces):

| Env var | Value | What it does |
|---|---|---|
| `VK_DRIVER_FILES` | `/home/agent/workspace/fluxer-local/gpu/nvidia_icd_egl.json` | Tells the Vulkan loader to use **`libEGL_nvidia.so.0` as the ICD** (NVIDIA's headless-friendly ICD; a user-space 164-byte JSON — nobody has to touch `/usr` or `/etc`). |
| `LD_LIBRARY_PATH` | `/home/agent/workspace/fluxer-local/gpu/extract-egl/usr/lib/x86_64-linux-gnu` | Supplies the **missing `libEGL.so.1`** (extracted from the 34.6 kB `libegl1_1.7.0-1+b2_amd64.deb` via `dpkg-deb -x`; no root). This also repairs the *default* `nvidia_icd.json` path, so apps that ignore `VK_DRIVER_FILES` still work — and any EGL user benefits. |

Notes:
- Old/bundled loaders that predate `VK_DRIVER_FILES` still accept the deprecated `VK_ICD_FILENAMES=<same json>` — verified honored by our loader too.
- **If a host/container-image change is ever possible, the clean fix is just: add `libegl1` to the container image** (optionally via nvidia-container-toolkit `NVIDIA_DRIVER_CAPABILITIES=graphics`). No driver/kernel change required. For kairo: nothing else needed on the host.
- Wrapper script `gpu/verify-gpu.sh` runs the quick self-test.

---

## 4. Verification (excerpts)

`vulkaninfo --summary` (excerpt; full file `gpu/verify_vulkaninfo_summary.txt`):

```
GPU0:
    apiVersion         = 1.3.277
    driverVersion      = 550.127.5.0
    vendorID           = 0x10de
    deviceID           = 0x1c03
    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName         = NVIDIA GeForce GTX 1060 6GB
    driverID           = DRIVER_ID_NVIDIA_PROPRIETARY
    driverName         = NVIDIA
    driverInfo         = 550.127.05
    conformanceVersion = 1.3.7.2
```

llama.cpp Vulkan build (`gpu/verify_llama_devices.txt`):
```
$ llama-cli --list-devices
Available devices:
  Vulkan0: NVIDIA GeForce GTX 1060 6GB (6390 MiB, 6302 MiB free)
```
Real generation ran through the GPU path (toy 260K model; llama-bench sanity: pp32 32 051 t/s, tg16 2 308 t/s — meaningless perf-wise, proves the stack).

whisper.cpp (CPU), jfk.wav, 11.0 s audio, `-t 4`:
```
And so my fellow Americans ask not what your country can do for you, ask what you can do for your country.
total time = 8995 ms   (~1.2× realtime; G4560 is only 2C/4T)
```

SmolVLM2-256M caption (Vulkan), 640×480 photo:
> "A black puppy is sitting on a wooden surface, looking directly at the camera…"  (~10 s incl. cold load)

piper TTS (CPU): 3.67 s of speech synthesized in 0.82 s → **RTF 0.223**, 22050 Hz mono WAV.

---

## 5. Model feasibility matrix (6 GB GTX 1060, prebuilt binaries only)

All artifacts below are prebuilt/binary — no compiling needed. VRAM figure = weights + KV headroom on this GPU.

### 5a. Via Vulkan on the GPU

| Modality | Pick | Download size | Runs at | Expected speed class | Source (exact) |
|---|---|---|---|---|---|
| LLM text (chat) | **llama.cpp Vulkan build** b10903 | 30.2 MB binary | GPU, full offload (default `-ngl 1024`) | 0.5–1B: 100+ t/s (est.) · 3B Q4: ~40–70 t/s (est.) · 7B Q4: **~25–35 t/s** (community: 28–90 t/s on this GPU) | `https://github.com/ggml-org/llama.cpp/releases/download/b10903/llama-b10903-bin-ubuntu-vulkan-x64.tar.gz` (pattern: `…/releases/download/<tag>/llama-<tag>-bin-ubuntu-vulkan-x64.tar.gz`; CPU twin: `-ubuntu-x64`, 16.8 MB) |
| LLM — 3B class model | Qwen2.5-3B-Instruct **Q4_K_M** | ~1.9 GB | fits w/ room for 8K ctx | as above | `bartowski/Qwen2.5-3B-Instruct-GGUF` on HF |
| LLM — 7B class model | e.g. Qwen2.5-7B / Llama-3.1-8B **Q4_K_M** | ~4.4–4.9 GB | fits **tight**; keep `-c 2048–4096`; full offload on 1060 6GB is community-verified at 4096 ctx | ~25–35 t/s (est.) | any GGUF repo on HF |
| Vision captioning (video frames!) | **SmolVLM2-256M-Video** Q4_K_M + mmproj Q8_0 | **131 MB + 104 MB = 235 MB** ✅ *installed & verified* | GPU; `llama-mtmd-cli -m … --mmproj … --image f.jpg` | caption ≈10 s inc. cold load for 640×480 (image encode ≈6 s; 0.16 s/chunk after warm-up) | `https://huggingface.co/ggml-org/SmolVLM2-256M-Video-Instruct-GGUF` |
| Vision captioning (better) | SmolVLM-500M Q8_0 + mmproj Q8_0 | 437 + 109 MB = 546 MB | GPU | ~1.5–2× slower than 256M, better sentences | `ggml-org/SmolVLM-500M-Instruct-GGUF` |
| Vision/OCR (best of the small) | Qwen2.5-VL-3B Q4_K_M + mmproj Q8_0 | 1.93 GB + 845 MB = 2.78 GB | GPU (tight w/ ctx) | better OCR/chart reading; slower (est. few s/image + 10–25 t/s) | `ggml-org/Qwen2.5-VL-3B-Instruct-GGUF`; use `llama-qwen2vl-cli` |
| Vision (alt.) | moondream2 (2025-04-14) f16 + mmproj | 2.84 GB + 910 MB = 3.75 GB | GPU | heavy for its class (f16 only) | `ggml-org/moondream2-20250414-GGUF` |
| Image generation | **stable-diffusion.cpp Vulkan prebuilt** + SD1.5 GGUF | 46.3 MB + 1.6–2.1 GB | GPU | estimates: SD1.5 Q8 ~20–40 s / 512² 20 steps; SDXL-Turbo fp16 (6.9 GB) too big for 6 GB — use Q4/Q5 GGUF quants or skip | `https://github.com/leejet/stable-diffusion.cpp/releases/download/master-853-b68d586/sd-master-b68d586-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip` · models: `second-state/stable-diffusion-v1-5-GGUF` (Q8_0 1.76 GB, f16 2.13 GB) |

### 5b. CPU (fallbacks / light tasks)

| Modality | Pick | Size | Expected speed (G4560) |
|---|---|---|---|
| STT | **whisper.cpp prebuilt CPU** b5130 ✅ *installed & verified* | 9.8 MB + models: `tiny.en` 77.7 MB (installed) / `base.en` ~142 MB / `small.en` ~466 MB | tiny.en **~1.2× realtime** (measured); base ~0.6×, small ~0.2× (est.). No official Linux Vulkan prebuilt — Vulkan needs a source build (later; GPU would be ~10× faster). |
| TTS | **piper** ✅ *installed & verified* | 26.5 MB + voice 63 MB | **RTF 0.22 measured** — instant |
| TTS (alt) | edge-tts (network, already in Hermes) | 0 | instant (cloud); Kokoro: venv has **onnxruntime** already, `kokoro-v1.0.onnx` ~310 MB, untested, needs `uv` venv |
| LLM | llama.cpp CPU build (16.8 MB) as fallback | models same as GPU | 3B Q4 ≈ 3–5 t/s; 7B Q4 ≈ 1.5–2.5 t/s (est.) — GPU strongly preferred |

GPU-only rows are the point of tonight; CPU rows are the "if the GPU is busy" escape hatch.

---

## 6. Recommended picks for our use

- **STT** → whisper.cpp `whisper-cli` (installed) + `base.en` for quality; keep `-t 4`. Build with `-DGGML_VULKAN=ON` later for speed (not tonight).
- **TTS** → piper (installed, RTF 0.22) for local; edge-tts as zero-setup alternative.
- **Vision / captioning / video frames** → SmolVLM2-256M-Video (installed, verified) as the default frame captioner; bump to SmolVLM-500M (546 MB) or Qwen2.5-VL-3B (2.78 GB) when quality matters. Pair with **ffmpeg 7.1.5** (already present) for frame extraction.
- **LLM** → llama.cpp Vulkan + one 3B Q4_K_M (~1.9 GB, comfortable) or 7B Q4_K_M (~4.7 GB, tight but 4096 ctx verified on this GPU class). Run `llama-bench` once after download for real numbers.
- **Image gen (optional)** → stable-diffusion.cpp Vulkan + SD1.5 Q8 GGUF; treat as experimental tonight.

---

## 7. Next commands (copy-paste)

```bash
cd /home/agent/workspace/fluxer-local
. gpu/gpu-env.sh                      # ALWAYS first, in each new shell

# --- LLM (download a real 3B model, ~1.9 GB) ---
curl -L -o models/Qwen2.5-3B-Instruct-Q4_K_M.gguf \
  "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf"
gpu/tools/llama-b10903/llama-cli   -m models/Qwen2.5-3B-Instruct-Q4_K_M.gguf -c 4096 -st
gpu/tools/llama-b10903/llama-bench -m models/Qwen2.5-3B-Instruct-Q4_K_M.gguf -p 512 -n 128   # real t/s

# --- Vision: caption any image ---
gpu/tools/llama-b10903/llama-mtmd-cli \
  -m models/smolvlm2-256m.gguf --mmproj models/mmproj-smolvlm2.gguf \
  --image models/test-dog.jpg -p "Describe this image." -n 64

# --- STT ---
gpu/tools/whisper-bin-ubuntu-x64/whisper-cli -m models/ggml-tiny.en.bin -f audio.wav -t 4
# quality upgrade: curl -L -o models/ggml-base.en.bin \
#   "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"

# --- TTS ---
echo "Hello from Fluxer." | gpu/tools/piper/piper \
  --model models/en_US-lessac-medium.onnx --output_file out.wav

# --- Image gen (optional, ~50 MB + 1.8 GB) ---
# curl -L -o tools/sd-vulkan.zip "https://github.com/leejet/stable-diffusion.cpp/releases/download/master-853-b68d586/sd-master-b68d586-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip"
# curl -L -o models/sd15-q8.gguf "https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF/resolve/main/stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf"
# (unzip; run the `sd` binary with --help to check flags)
```

---

## 8. Gotchas

- **Every new shell needs `. gpu/gpu-env.sh`** before GPU apps (or set the two vars another way). Without it, Vulkan fails with the old `ERROR_INCOMPATIBLE_DRIVER`.
- The fix depends on files under `gpu/` (`nvidia_icd_egl.json`, `extract-egl/`) — don't delete them.
- Keep 7B-class models at `-c ≤4096` on this 6 GB card; watch `nvidia-smi` if OOM.
- `llama.cpp` Linux CUDA prebuilts don't exist (Vulkan does; that's fine — Vulkan TG on Pascal reportedly beats older CUDA builds).
- whisper.cpp has no official Linux Vulkan prebuilt; CPU tiny/base is the tonight answer, source build later.
- GPU identity is **GTX 1060 6GB**, not 1650 — expectations should match Pascal.

---

## 9. Appendix — files created tonight

| Path (under `/home/agent/workspace/fluxer-local/`) | What |
|---|---|
| `gpu/gpu-env.sh` | **the recipe** — source this |
| `gpu/nvidia_icd_egl.json` | user-space ICD manifest → `libEGL_nvidia.so.0` |
| `gpu/extract-egl/usr/lib/x86_64-linux-gnu/libEGL.so.1*` | GLVND `libEGL.so.1` extracted from `libegl1` (plan B) |
| `gpu/libegl1_1.7.0-1+b2_amd64.deb` | the 34.6 kB package (source of plan B) |
| `gpu/verify-gpu.sh` | 10-second self-test |
| `gpu/probe_icd.py`, `gpu/probe_cuda.py` | diagnostics (Vulkan ICD / CUDA init) |
| `gpu/vulkan_debug.log`, `gpu/vulkan_lddebug.log`, `gpu/probe_symbols.log` | diagnostic logs |
| `gpu/verify_vulkaninfo_summary.txt`, `gpu/verify_llama_devices.txt` | saved verification output |
| `gpu/nvcompat_shim.c`, `gpu/libnvcompat.so` | dead-end shim (red herring; not needed) |
| `gpu/tools/llama-b10903/` | llama.cpp b10903 Vulkan build (llama-cli, llama-bench, llama-mtmd-cli, llama-qwen2vl-cli, llama-server, …) |
| `gpu/tools/whisper-bin-ubuntu-x64/` | whisper.cpp b5130 (whisper-cli, whisper-server, …) |
| `gpu/tools/piper/` | piper TTS binary |
| `models/` | stories260K.gguf, ggml-tiny.en.bin, jfk.wav, smolvlm2-256m.gguf, mmproj-smolvlm2.gguf, test-dog.jpg, en_US-lessac-medium.onnx(+json), piper-test.wav |
