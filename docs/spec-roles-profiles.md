# Spec: realtime/omni engine — role-tagged profiles for duplex multimodal

> **See also:** `docs/paradigm-profiles.md` — the four core operating profiles
> (OMN, OMNI-THINKER-TALKER, PIECEMEAL-OMNI, TURN-BASIC) that this spec
> grammar enables. This document defines the machinery; the paradigm doc
> defines the actual profiles that use it.

## Scope statement

This document defines the **realtime/omni engine** — a Hermes-compatible plugin that provides role-tagged multimodal backends (ears, mouth, eyes, talker, thinker, realtime, omni), a profile system for composing them, and a streaming duplex architecture for voice/video.

It is **separate from the fluxer chat platform plugin** (`docs/spec-fluxer-plugin.md`). The realtime engine has no dependency on fluxer — any Hermes platform plugin (discord, telegram, fluxer) can use it. fluxer can optionally bridge to it for voice; that bridge is documented in §7 of fluxer's spec.

### What this engine IS
- A backend registry with typed role interfaces and a profile grammar
- A session FSM that coordinates listening, thinking, speaking, and preemption
- A cancellation protocol that lets any active backend be interrupted mid-turn
- A fallback chain mechanism per role slot for degraded-mode operation
- A transport-agnostic realtime audio/video pipeline (LiveKit is the reference implementation)

### What this engine IS NOT
- **Not a model manager.** Local model installation, GGUF fetching, and binary configuration are user behavior — documented in the feasibility guide (`docs/omni-models-feasibility.md`) and user-facing deployment docs, not in the plugin code. The engine defines interface contracts; the user provides implementations.
- **Not a chat platform adapter.** Text messaging, channels, guilds, and attachments belong to platform plugins (fluxer, discord, etc.). The engine handles senses — text is just another sense.

---

## 1. Role taxonomy

| Role | Direction | Job | Example backends |
|------|-----------|-----|------------------|
| `ears` | audio in | speech → text | whispercpp, qwen3asr_server, saas_stt |
| `eyes` | image/video in | see → describe | smolvlm2, qwen2.5-omni-3b, saas_vision |
| `mouth` | audio out | text → speech | piper, qwen3tts, saas_tts |
| `talker` | text in/out | fast front-end; speaks for the system | agent (brief mode), smaller LLM, s2s |
| `thinker` | text in/out | deep reasoning; owns tools/context | the Hermes agent (full config), bigger LLM |
| `realtime` | audio in+out | duplex speech; bypasses cascade when present | mini-omni2, openai_realtime, crypasr |
| `omni` | all | everything front-to-back (god model) | future single model via wss:// |
| `text` | text in/out | plain text fallback | local llama-server, hermes agent |

Each backend must declare which roles it can fill. A single backend (e.g. a Speech-to-Speech model like Mini-Omni2) can declare `[ears, mouth, realtime]`. An LLM can declare `[talker, thinker, text]`.

---

## 2. Backend interface protocol

Every backend implements a typed protocol. The engine calls these methods; backends never call back into the engine. The caller is always the engine's session controller.

### 2.1 `AudioIn` (ears)

```python
class AudioIn:
    async def transcribe(self, wav_bytes: bytes, *, sample_rate: int = 16000) -> str: ...
    async def transcribe_stream(self, chunk_stream: AsyncIterator[bytes]) -> AsyncIterator[str]: ...
```

- `transcribe` accepts a complete WAV (or raw PCM with format header) and returns the transcript.
- `transcribe_stream` accepts streaming PCM chunks and yields incremental text (for live captioning).
- Default: `transcribe`, no streaming requirement. Streaming is an optimization for low-latency ears.

### 2.2 `AudioOut` (mouth)

```python
class AudioOut:
    async def synthesize(self, text: str, *, voice: str | None = None) -> bytes: ...
    async def synthesize_stream(self, text_stream: AsyncIterator[str], *, voice: str | None = None) -> AsyncIterator[bytes]: ...
    async def cancel(self) -> None: ...
```

- `cancel()` must stop synthesis mid-word and return as quickly as possible. It is the preemption mechanism. A backend that cannot cancel (e.g. a blocking subprocess call) must document its limitation; the engine will fall back to ignoring its output and starting fresh.

### 2.3 `Vision` (eyes)

```python
class Vision:
    async def describe(self, image_bytes: bytes, *, prompt: str | None = None) -> str: ...
    async def describe_frames(self, frames: AsyncIterator[bytes], *, fps: float = 0.5) -> AsyncIterator[str]: ...
```

- `describe_frames` receives a stream of JPEG/PNG frames (sampled externally, e.g. by ffmpeg's scene filter) and yields descriptions as they land. The engine only calls this for live screen/webcam feeds.

### 2.4 `Text` (talker / thinker)

```python
class Text:
    async def chat(self, messages: list[dict], *, brief: str | None = None) -> AsyncIterator[str]: ...
    async def cancel(self) -> None: ...
```

- The `messages` list follows the standard Hermes message format (role/content alternation). The `brief` parameter injects a role-specific system preface ("you are the fast front-end, keep answers short...").
- The `agent` backend resolves to `platform_adapter.handle_message()`. The `brief` maps to an injected system-message suffix in the session context.
- For the `thinker` role, `brief` is the default system prompt (Hermes' full persona + tool set).
- For the `talker` role, `brief` is "you are the responsive front-end — keep answers conversational and under 3 sentences. Let the thinker handle deep questions."

### 2.5 `Realtime` (duplex speech)

```python
class Realtime:
    async def start_session(self, *, mic: AsyncIterator[bytes], speaker: AsyncIterator[bytes] | None = None) -> RealtimeSession: ...
    async def stop(self) -> None: ...
    @property def can_interrupt(self) -> bool: ...
```

- `RealtimeSession` is a bidirectional stream object with `async def read_output() -> AsyncIterator[bytes]` (audio from the model) and `async def feed_input(chunk: bytes)` (audio to the model). The engine wraps this in the session FSM.
- `can_interrupt` signals whether the backend supports barge-in. If false, the session controller falls to half-duplex mode.
- The Mini-Omni2 torch path (validated in C7) implements this slot via `run_AT_batch_stream` wrapped in an asyncio generator. The reference implementation is `fluxer.voice.miniomni2_realtime.MiniOmni2Realtime`.

### 2.6 `Omni` (god model — future)

Reserved. Protocol sketch:

```python
class Omni:
    async def connect(self, endpoint: str, *, config: dict) -> OmniSession: ...
    async def disconnect(self) -> None: ...
```

The OmniSession provides typed I/O streams per sense. Not specified until a concrete model exists.

---

## 3. Profile grammar

A profile binds roles to backends and sets composition/tempo.

```yaml
profiles:
  # Example: fully stitched cascade
  split-local:
    mode: stitched                # stitched | unified
    tempo: fast_half_duplex       # turn | fast_half_duplex | realtime
    slots:
      audio_in:  { backend: local.whispercpp, fallback: local.qwen3asr_server }
      audio_out: { backend: local.piper, fallback: local.qwen3tts }
      eyes:      { backend: local.smolvlm2, push_fps: 0.5 }
      talker:    { backend: agent, brief: "fast-front" }
      thinker:   { backend: agent, brief: "default" }

  # Same hardware, uses unified S2S when available
  realtime-capable:
    mode: unified
    tempo: realtime
    slots:
      realtime:  { backend: local.miniomni2, can_interrupt: true }
      eyes:      { backend: local.smolvlm2, push_fps: 0.3 }
      thinker:   { backend: agent }
```

### 3.1 Slot resolution

The engine resolves a profile to active backends on session start:

1. For each role declared in the profile, look up the named backend in the registry.
2. If a role is absent but required (e.g. `ears` in stitched mode), try the fallback chain: every slot that declares a `fallback` list is tried in order on failure.
3. If the profile declares `realtime` or `omni` with tempo `realtime`, bypass stitched cascade entirely — the unified backend handles audio in/out. Stitched roles (ears, mouth, talker) remain registered for text/info relay.
4. Roles without a backend resolve to a `null` backend that always returns an appropriate error ("no eyes configured").

### 3.2 Tempo

- `turn` — full round-trip: listen all, think, speak all. No streaming, no preemption. Oldest and simplest.
- `fast_half_duplex` — the cascade is tuned for responsiveness: streaming ASR, chunked LLM output (TTS starts on first token), aggressive VAD silence window. Interruption drops the current turn and restarts. This is the production target for the GGUF cascade on this hardware.
- `realtime` — the realtime slot handles audio in/out directly. The session FSM runs in duplex mode: listen-while-speak enabled, interruption is per-frame. Actual performance depends on backend capability.

---

## 4. Session FSM

All three pipelines (audio in, talker/thinker, audio out) are coordinated by a single session-level finite state machine:

```
States:
  IDLE        — no activity, waiting
  LISTENING   — audio_in is active, collecting utterance
  ANALYZING   — VAD closed; audio_in producing transcript
  THINKING    — talker/thinker active, generating text
  SPEAKING    — audio_out active, playing speech
  PREEMPTING  — interruption detected; cancelling in-flight work
  HALTED      — unrecoverable error, session dead
```

### Transition rules

| From | Event | To | Action |
|------|-------|----|--------|
| IDLE | VAD open (speech start) | LISTENING | Start audio_in stream, start pre-buffer ring |
| LISTENING | VAD close (silence ≥ threshold) | ANALYZING | Close audio_in, request transcript |
| ANALYZING | transcript ready | THINKING | Inject transcript into agent context |
| THINKING | first token ready | SPEAKING | Start audio_out_stream in parallel |
| THINKING | full answer ready (no SPEAKING yet) | SPEAKING | Start audio_out |
| SPEAKING | VAD open (interruption) | PREEMPTING | Call `cancel()` on audio_out + talker/thinker |
| SPEAKING | audio_out stream ends naturally | IDLE | Log complete turn |
| PREEMPTING | all cancel() calls return | LISTENING | Start new audio_in (pre-buffer ring has trailing audio) |
| PREEMPTING | cancel() timeout (2s) | LISTENING | Force-close backends, start new audio_in |
| * | fatal backend error | HALTED | Report, no auto-recovery |

### Pre-buffer ring

During any state, the session maintains a ring buffer of the last 640 ms of raw PCM audio (10240 bytes @ 16 kHz). This ensures that an interruption triggered mid-speech does not eat the beginning of the user's utterance. When transitioning PREEMPTING→LISTENING, the ring buffer is flushed into the new audio_in stream as the first chunk.

---

## 5. Cancellation protocol

Every backend that can produce output (mouth, talker, thinker, realtime, omni) must implement `cancel()`. Backends that block (subprocess calls, synchronous loops) should document their interrupt latency. The engine enforces:

- `cancel()` is called on all active backends when the session enters PREEMPTING.
- If a backend does not return from `cancel()` within 2 seconds, the session controller marks it as unresponsive and proceeds with the remaining backends.
- After `cancel()`, the backend is reset to a clean state. If the same backend is called again in the same turn (e.g. an interrupted TTS followed by a new TTS from the new turn), it must accept new input immediately.
- Backends that cannot cancel (subprocess spawning without pipe) must raise `UncancelableError`. The engine then ignores their output (it will arrive eventually but is discarded) and starts the new pipeline.

---

## 6. Fallback chains

Every slot declaration may include a `fallback` list (ordered by preference):

```yaml
audio_in:
  backend: local.qwen3asr_server
  fallback:
    - local.whispercpp
    - saas_google_stt
```

On failure:
1. The primary backend reports a typed error (connection refused, OOM, timeout, invalid response).
2. The session controller logs the error and tries the first fallback, then the second, etc.
3. If all backends in the chain fail for the same role, the slot is degraded to `null`. The session emits a notification ("ears not available — interaction will be text-only") and continues.
4. Each backends in the chain inherits the same configuration as the primary (URL, model path, API key) unless overridden by a backend-specific `config` map in the slot.

---

## 7. User behavior: local models

Local models (whisper.cpp, piper, llama.cpp, Mini-Omni2, SmolVLM2, etc.) are **not managed or shipped by the engine**. The engine defines backend interfaces (§2); the user installs and configures concrete backends.

The expected workflow:
1. Decide which backends you want (e.g. "I want local ears + remote thinker + local mouth").
2. Install the model software (whisper.cpp, llama.cpp, piper, etc.) according to the deployment guide (`docs/omni-models-feasibility.md` or your own docs).
3. Write a short config mapping backend names to binary paths / server URLs:
   ```yaml
   backends:
     local.whispercpp:
       type: subprocess
       binary: /opt/whisper/whisper-cli
       model: /opt/models/ggml-tiny.en.bin
     local.qwen3asr_server:
       type: http
       url: http://127.0.0.1:8105/v1/audio/transcriptions
     local.miniomni2:
       type: python
       module: fluxer.voice.miniomni2_realtime
   ```
4. Reference these backends in your omni profile (§3).

The engine validates at session start: backend type recognized? Binary exists? Server URL reachable? Module importable? Failures give a clear message ("backend local.whispercpp: binary not found at /opt/whisper/whisper-cli").

---

## 8. Features toggle for platform plugins

Any Hermes platform plugin (fluxer, discord, telegram) can activate the realtime engine by toggling a config feature flag:

```yaml
# In the platform's config section (e.g. fluxer config)
fluxer:
  features: [realtime]        # activates the realtime engine
  omni_profile: split-local   # which profile to use
```

When the feature is toggled on but the engine package is not installed, the platform adapter emits a clear error at startup: "realtime feature requires the realtime/omni engine plugin (pip install hermes-realtime)" and continues in text-only mode.

When the engine is active, the platform adapter:
- Reserves a voice channel (via `update_voice_state` or platform-native voice API)
- Passes audio streams to the engine's session controller
- Routes engine output (speech, text responses, transcripts) to the appropriate channel

---

## 9. Match-pattern orchestration (internal engine logic, not user-facing)

The engine resolves the active profile to a runtime composition using a match over role presence:

```python
match (has_realtime, has_talker, has_thinker, has_eyes, has_ears, has_mouth, tempo):
    (true, _, _, _, _, _, _) =>
        # realtime backend handles audio in/out on its own.
        # talker/thinker registered for text queries.
        # eyes on standby via tool call.
    (false, true, true, _, true, true) & fast_half_duplex =>
        # split mode: ears → talker (fast) + thinker (deep, async).
        # mouth starts when talker yields first token.
    (false, false, true, _, true, true) =>
        # cascade: ears → thinker → mouth (no talker split).
    (false, _, _, _, true, false) =>
        # ears → thinker → text-only (no mouth).
    (_, _, _, true, _, false) =>
        # eyes registered as describe_visual tool, on-demand only.
```

This is engine internal logic. Users express what they want via the profile (§3); the engine determines how to wire it.

---

## 10. Implementation plan

1. **Backend registry** — typed protocols per role (§2). Base classes with `async def` signatures. A registry map `backend_name → instance`.
2. **Profile resolver** — parse YAML profile into slot bindings. Resolve fallbacks. Validate all referenced backends exist.
3. **Session FSM** — `asyncio.Future`-based state machine with cancel propagation. Pre-buffer ring in shared memory.
4. **Transport layer** — LiveKit-based audio transport (reference impl: `fluxer/voice/livekit_transport.py`). Abstract transport interface for alternative providers.
5. **Platform bridge** — a thin mixin class that any `PlatformAdapter` can use to enable realtime. Mixin handles feature toggle, voice-channel lifecycle, audio relay.
6. **Tests** — mock backends for every role; FSM transition coverage; fallback chain unit test; livekit mock for transport layer. No hardware-dependent tests (use null backends).

---

## 11. Open questions

- **Multiplexing:** Can a single backend instance service multiple concurrent sessions? The engine currently assumes 1:1 session-to-backend mapping. For cloud STT/TTS APIs this is wasteful; for local GPU binaries it's dangerous. A future `pool_size` param on backend declarations would help.
- **Talker/thinker async split**: The spec supports `talker_answers → thinker_followup` as a strategy, but the detailed timing (when does the thinker fire? only if talker defers? always but overlapped?) is unresolved.
- **Eyes standby vs push**: The spec supports both modes, but the profile YAML only expresses push (`push_fps`). On-demand (`describe_visual` tool) is currently wired outside the profile system (as a Hermes tool). Should the tool be auto-generated from the profile?
- **Nothing downstream cares whether one model or five serve the senses** — this claim holds for the text output path but is unproven for realtime streaming. Can a stitched cascade (ears→talker→mouth) match the uninterrupted flow of a unified realtime model? The auditor was skeptical. Testing this comparison is deferred until both paths exist.