# Paradigm profiles — the four omni engine operating modes

> **Cross-reference:** See `docs/spec-roles-profiles.md` for the role taxonomy and profile
> grammar this document builds on. See `docs/omni-models-feasibility.md` for the
> hardware reality (GTX 1060 6GB, GGUF runtime, verified backends) that grounds
> the resource requirements quoted here.

The omni engine defines **four paradigm profiles** that span the full spectrum of
multimodal interaction — from single-model full-duplex to simple turn-based text.
Each profile is a concrete YAML config that can be dropped into the `omni.profiles`
section of `sandbox/hermes-home/config.yaml`.

| # | Name | Mode | Tempo | Duplex |
|---|------|------|-------|--------|
| 1 | `omn` | unified | realtime | full |
| 2 | `omni-thinker-talker` | stitched (with paired roles) | realtime | split — duplex frontend, async deep backend |
| 3 | `piecemeal-omni` | stitched | fast\_half\_duplex | best-of-breed cascade |
| 4 | `turn-basic` | unified | turn | none |

---

## 1. OMN — single omni model, 4-in-2-out full duplex

### Config

```yaml
omn:
  mode: unified
  tempo: realtime
  backend: local.crispasr_miniomni2
  senses: [text, audio, image, video]
  options:
    can_interrupt: true
    tool_delegation: subagent          # async tool calls via Hermes subagents
```

### What it does

One model handles all four senses (text, audio, image, video) bidirectionally —
speech in, speech out, vision, text — in a single duplex session. The engine
wraps the unified backend as a `DuplexSession`; there is no cascade between STT,
LLM, and TTS. Audio in and out happen concurrently (listening-while-speaking).

### Backends

| Role | Backend | Notes |
|------|---------|-------|
| All-in-one | `local.crispasr_miniomni2` | CrispASR Vulkan-accelerated mini-omni2 (~1.5 GB VRAM). Future: any `DuplexBackend` (openai\_realtime, a local omni model via WebSocket). |

### Tempo / mode

- **mode:** `unified` — one backend, one `DuplexSession` seam.
- **tempo:** `realtime` — the session FSM runs in duplex mode. The backend
  manages the listen-while-speak loop natively via its `RealtimeSession`
  protocol. The engine's FSM stays in `LISTENING + SPEAKING` concurrently.

### Interruption

Native barge-in. The crispasr backend reports `can_interrupt = true`. When the
engine's VAD detects speech during model output, it calls `cancel()` on the
backend, which stops synthesis and switches to listening mode. The 640 ms pre-
buffer ring preserves leading audio across the transition.

### Async tool delegation

Since the omni model is a unified audio-in/audio-out pipeline, it cannot do
traditional tool calls (which require the Hermes agent's plugin ecosystem).
Tools are delegated to subagents via an async side channel:

```
User audio → omn backend → [tool intent detected]
                                ↓
                    Hermes subagent (tool executor)
                                ↓
                    result injected back into context
                                ↓
                    omn backend continues response
```

This is configured via `options.tool_delegation: subagent`. The engine spawns a
subagent session on a separate context, collects the tool result, and feeds it
back as an in-band system message.

### Resource requirements

- **VRAM:** ~1.5–2.5 GB (crispasr mini-omni2 Q4, SNAC codec). Future omni
  models (Qwen2.5-Omni-3B GGUF): ~3.5 GB including mmproj.
- **CPU:** Light — most work is GPU (Vulkan/CUDA). Talker bridge uses the
  Hermes agent process for tool delegation.
- **Disk:** Model GGUF ~1–3.5 GB depending on backend.

---

## 2. OMNI-THINKER-TALKER — duplex fast frontend + deep async thinker

### Config

```yaml
omni-thinker-talker:
  mode: stitched
  tempo: realtime
  bindings:
    audio_in:  { backend: local.crispasr_miniomni2, options: { duplex_role: talker } }
    audio_out: { backend: local.crispasr_miniomni2, options: { duplex_role: talker } }
    talker:    { backend: agent, options: { brief: "fast-front" } }
    thinker:   { backend: agent, options: { brief: "default" } }
    image_in:  { backend: local.smolvlm }
    video_in:  { backend: local.smolvlm_video }
```

### What it does

A fast duplex frontend (the "talker") handles realtime audio in/out — 4 senses
in, 2 senses out — but keeps responses short and conversational. When the
talker decides a question needs real reasoning, it says "let me check" and
delegates to a "thinker" backend that has the full Hermes agent context,
tools, and longer generation budget. The thinker receives the raw media
context (audio waveform, image bytes, not just text summaries) so it can
reason from the same sensory evidence.

The talker and thinker run as paired `text` role slots. The engine's session
FSM manages the handoff:

```
User audio → talker (crispasr) → [query is simple?] → talker answers directly
                                  [query needs depth] → talker says "let me check"
                                                          ↓
                                                    thinker (full agent)
                                                          ↓
                                                    talker relays answer
```

### Backends

| Role | Backend | Notes |
|------|---------|-------|
| audio\_in | `local.crispasr_miniomni2` | Full-duplex realtime ASR. Also provides the talker's audio context. |
| audio\_out | `local.crispasr_miniomni2` | Full-duplex realtime TTS (same model, different direction). |
| talker | `agent` (brief:"fast-front") | The Hermes agent with a short, conversational system prompt. Kept responsive by low token budget. |
| thinker | `agent` (brief:"default") | The Hermes agent with full persona, tool set, and deep context. Can execute multi-step plans. |
| image\_in | `local.smolvlm` | SmolVLM2 for still image captioning (GPU). |
| video\_in | `local.smolvlm_video` | SmolVLM2 for video frame captioning (GPU). |

### Tempo / mode

- **mode:** `stitched` — multiple backends wired together.
- **tempo:** `realtime` — the audio pathway (talker) runs in full duplex. The
  thinker operates asynchronously on a separate logical channel.

### Talker → thinker async bridge

The bridge is the engine's central innovation in this profile:

1. **Talker receives** audio + optional vision → transcribes + brief response.
2. **Talker classifies** the query: if it needs tools, multi-step logic, or
   context beyond the talker's brief.
3. **Talker defers** by emitting a bridging token ("let me check") while
   forwarding the **raw media** (PCM audio shard, image/video frame bytes)
   to the thinker slot.
4. **Thinker processes** the media plus the talker's context, runs tools,
   calls subagents, and streams its result back.
5. **Talker relays** the thinker's answer as speech, seamlessly continuing.

The async bridge is non-blocking: the talker can handle a new user utterance
while the thinker is still computing the previous one (interleaved turns).

Configuration via `thinker_options` (not shown in the minimal config above):

```yaml
thinker: { backend: agent, options: { brief: "default" }, thinker_options:
  { timeout: 120, max_tool_calls: 10, relay_raw_audio: true, relay_raw_image: true } }
```

### Interruption

The talker side supports native barge-in (same as OMN profile). When the user
interrupts the thinker's relayed speech, the talker stops, listens to the new
utterance, and may cancel the in-flight thinker turn. The thinker runs in a
separate asyncio task and can be `cancel()`-ed by the engine's FSM when it
enters PREEMPTING. Because the thinker may have side effects (tool calls that
already committed), the engine logs the cancellation and marks the turn as
abandoned rather than rolled back.

### Resource requirements

- **VRAM:** ~2.5–4.5 GB total:
  - crispasr mini-omni2: ~1.5 GB
  - SmolVLM2: ~1 GB (when active, can be offloaded on-demand)
  - Thinker (agent): 0 (uses Hermes agent's own LLM backend, which may be
    API or local GGUF consuming separate VRAM)
- **CPU:** Moderate — the thinker path may do heavy processing (tool execution,
  RAG, multi-step reasoning).
- **Disk:** ~2.5 GB for L2 models (crispasr + smolvlm). Thinker uses Hermes'
  existing LLM setup.

---

## 3. PIECEMEAL-OMNI — stitched best-of-breed per sense (fake realtime)

### Config

```yaml
piecemeal-omni:
  mode: stitched
  tempo: fast_half_duplex
  bindings:
    audio_in:  { backend: local.qwen3asr, fallback: [local.whispercpp] }
    audio_out: { backend: local.qwen3tts, fallback: [local.piper] }
    image_in:  { backend: local.smolvlm }
    video_in:  { backend: local.smolvlm_video }
    talker:    { backend: agent, options: { brief: "fast-front" } }
    thinker:   { backend: agent, options: { brief: "default" } }
    text_in:   { backend: agent }
    text_out:  { backend: agent }
```

### What it does

Each sense gets its own purpose-built backend — the **best available** for that
modality. Instead of one model doing everything, a stitched pipeline runs
ASR → LLM → TTS with dedicated SOTA components per stage:

- **Ears:** Qwen3-ASR 0.6B GGUF (GPU, llama.cpp, verified) — far better accuracy
  than whisper tiny, runs in 1.5 GB VRAM.
- **Mouth:** Qwen3-TTS 1.7B GGUF (GPU, llama-tts, verified) — voice cloning,
  24 kHz output, ~2.5 GB VRAM.
- **Eyes:** SmolVLM2-256M (GPU, llama-mtmd-cli, verified) — image and video
  captioning.
- **Brain:** The Hermes agent (talker/thinker) with a central snappy text model
  (e.g. Qwen2.5-3B Q4_K_M on GPU, or an API model) that orchestrates
  everything.

The "snappy central model" is configured via the `talker` and `thinker` slots
— talker provides fast conversational responses, thinker handles deep reasoning
with full tool access.

### Clever interruption handling

Because each component is a separate subprocess/HTTP call, interruption is
implemented as a **cascade cancel**:

1. VAD detects speech during TTS → engine enters PREEMPTING.
2. `llama-tts` / `piper` subprocess is killed (SIGTERM, then SIGKILL after
   500 ms grace).
3. In-flight ASR is not cancelled — it finishes and its output is discarded
   (the pre-buffer ring has the new utterance anyway).
4. The talker/thinker generation is cancelled via `cancel()`.
5. New audio stream starts from the pre-buffer ring.

The profile's `tempo: fast_half_duplex` means the engine optimizes for
responsiveness: streaming ASR output, speculative TTS start on first token,
aggressive VAD silence window (configurable, default 600 ms).

### Tempo / mode

- **mode:** `stitched` — independent backends per sense-direction slot.
- **tempo:** `fast_half_duplex` — tuned for responsive half-duplex operation
  with clever interruption that approaches realtime feel. True duplex
  (listen-while-speak) is not possible because the cascade is serial.

### Backends

| Role | Backend | Notes |
|------|---------|-------|
| audio\_in | `local.qwen3asr` (primary), `local.whispercpp` (fallback) | GPU ASR via llama.cpp (verified, 0.77 GB GGUF). Whisper CPU fallback. |
| audio\_out | `local.qwen3tts` (primary), `local.piper` (fallback) | GPU TTS with voice cloning (verified, 1.41 GB). Piper CPU fallback. |
| image\_in | `local.smolvlm` | SmolVLM2 GPU (0.48 GB GGUF + mmproj). |
| video\_in | `local.smolvlm_video` | Same model, native `--video` mode. |
| talker | `agent` (brief:"fast-front") | Fast conversational text model. |
| thinker | `agent` (brief:"default") | Full agent for deep reasoning and tool execution. |

### Resource requirements

- **VRAM:** ~2.5–4.5 GB total (varies by active component):
  - Qwen3-ASR: ~1.5 GB (transient, freed after transcribe)
  - Qwen3-TTS: ~2.5 GB (transient, freed after synthesize)
  - SmolVLM2: ~1 GB (transient)
  - Talker/thinker LLM: 0–3 GB depending on model (can be API to stay at 0)
  - Peak concurrent: ~4.5 GB if ASR + TTS + vision are all active (rare).
- **CPU:** Moderate — whisper fallback is CPU-only; piper fallback is CPU-only.
- **Disk:** ~2.5 GB for sense models (ASR + TTS + SmolVLM2 GGUFs). The LLM is
  Hermes' existing model.

### Why "fake realtime"

This profile looks realtime at the macro level (sub-second ASR, streaming TTS
start) but is strictly half-duplex — the pipeline processes one complete turn
at a time. The clever interruption creates a realtime *feel* by aggressively
aborting and restarting, but true duplex (listening while speaking) requires
a single unified model or the talker-thinker split (profile 2).

---

## 4. TURN-BASIC — simple turn-based model

### Config

```yaml
turn-basic:
  mode: unified
  tempo: turn
  backend: local.crispasr_miniomni2
  senses: [text, audio]
  fallback:
    image_in: { backend: local.smolvlm }
    video_in: { backend: local.smolvlm_video }
```

### What it does

The simplest possible omni profile: one model, one sense at a time, no
streaming, no preemption. Every interaction is a complete round-trip:
listen all → think all → speak all. Missing senses (image, video) are patched
in via separate fallback backends when the user provides that media type.

Designed for:
- Low-resource / edge devices (Pi, thin client)
- Accessibility mode (one-handed text entry with optional voice)
- Testing and debugging the engine's unified backend seam
- Users who want omni capability without any realtime complexity

### Tempo / mode

- **mode:** `unified` — one `DuplexBackend` for text and audio.
- **tempo:** `turn` — the session FSM processes one complete turn:
  `LISTENING → ANALYZING → THINKING → SPEAKING → IDLE`. No streaming, no
  preemption. The FSM does not enter PREEMPTING because interruption is not
  handled internally — it is managed **externally** by the platform adapter.

### Interruption

Interruption is **externally managed**. The profile itself has no internal
interruption mechanism: the FSM will not cancel a running backend mid-turn.
Instead, the platform adapter (fluxer, discord, telegram) decides when to
abort a turn:

- A new user message arrives → the adapter drops the current session and
  starts fresh (the `DuplexSession.close()` / `open()` cycle).
- The adapter may implement its own timeout (e.g. "user didn't speak for 30 s
  → cancel current turn").
- This makes "interruption" a property of the deployment platform, not the
  omni engine. The profile's `tempo: turn` explicitly signals that the engine
  does **not** own the interruption policy.

### Missing sense fallbacks

Because the profile only declares `senses: [text, audio]`, the unified backend
will reject `image` and `video` parts with a graceful error. The `fallback`
section patches those senses in at the session level:

```yaml
fallback:
  image_in: { backend: local.smolvlm }
  video_in: { backend: local.smolvlm_video }
```

Before the engine rejects an image/video part, it checks the `fallback` map
for that slot and routes to the sense backend instead. The unified model still
handles text and audio; image and video are processed out-of-band and the
description text is injected into the model's context.

### Backends

| Role | Backend | Notes |
|------|---------|-------|
| Primary (text + audio) | `local.crispasr_miniomni2` | Same as OMN, but used in turn mode — no streaming, no duplex. |
| image\_in (fallback) | `local.smolvlm` | Only activated when user sends an image. |
| video\_in (fallback) | `local.smolvlm_video` | Only activated when user sends video. |

### Resource requirements

- **VRAM:** ~1.5 GB (crispasr mini-omni2). +1 GB transient when SmolVLM2 is
  triggered for image/video.
- **CPU:** Minimal — all heavy work is GPU.
- **Disk:** ~1.5 GB for primary model + ~0.5 GB for fallback models.

---

## Comparison summary

| Property | OMN | OMNI-THINKER-TALKER | PIECEMEAL-OMNI | TURN-BASIC |
|----------|-----|---------------------|----------------|------------|
| **Mode** | unified | stitched (paired roles) | stitched | unified |
| **Tempo** | realtime | realtime | fast\_half\_duplex | turn |
| **Duplex** | full (listen+speak) | full (talker) + async thinker | half-duplex | none |
| **Components** | 1 model | 3 models + agent in 2 roles | 4+ models + agent | 1 model + fallbacks |
| **Interruption** | native barge-in | native + thinker cancel | cascade cancel (abort) | external (platform) |
| **Tool use** | subagent side-channel | thinker runs tools | thinker runs tools | thinker runs tools |
| **VAD** | engine-managed | engine-managed | engine-managed | platform-managed |
| **VRAM** | 1.5–3.5 GB | 2.5–4.5 GB | 2.5–4.5 GB | 1.5–2.5 GB |
| **Use case** | native duplex voice | smart assistant | production cascade | testing/accessibility |
| **Complexity** | low | high (bridge protocol) | medium | lowest |

---

## Relationship to existing profiles

The three pre-existing profiles in `sandbox/hermes-home/config.yaml` are
implementation variants of the paradigms above:

| Existing profile | Closest paradigm | Difference |
|-----------------|------------------|------------|
| `split-local` | `piecemeal-omni` | Uses whispercpp + piper (CPU) instead of Qwen3 ASR/TTS (GPU). No talker/thinker split. |
| `omni-unified` | `omn` | Same paradigm; existing name kept for backward compatibility. |
| `torch-unified` | `omn` (variant) | Uses torch Mini-Omni2 backend instead of CrispASR. A "path B" implementation of the OMN paradigm. |

The four paradigm profiles can coexist alongside the three existing profiles
in the config — they are mutually selectable via `omni.default_profile`.