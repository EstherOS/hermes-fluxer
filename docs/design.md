# Omni Engine — Design v2

## Separation of concerns

The system has three distinct layers, each with a defined boundary.

### 1. Fluxer plugin (hermes-fluxer)

A Hermes platform adapter that connects the Fluxer chat/voice API to the Hermes agent. Its responsibilities:

- **Platform protocol**: WebSocket gateway for Fluxer guilds, DMs, voice channels. REST API for message send/delete, channel state, member info.
- **Media transport**: Download attachments, upload files, bridge LiveKit WebRTC audio/video.
- **Agent integration**: Translate Fluxer message events into Hermes

`MessageEvent` format. Route text from the voice channel's embedded text channel into the agent's message queue.

- **Voice dispatch**: When a voice channel has audio, push PCM frames into the omni engine. When the engine produces audio, pull PCM frames out and publish to the LiveKit speaker track.

The Fluxer plugin is the **outer shell**. It knows about Fluxer's API, LiveKit WebRTC, and the Hermes agent protocol. It does not know about models, profiles, ASR, TTS, or component graphs — those are the engine's job.

### 2. Omni engine (hermes-omni)

A transport-agnostic realtime/multimodal engine. It has no platform dependencies — it works the same whether the audio comes from Fluxer, Discord, a file, or a microphone. Its responsibilities:

- **Profile system**: Read a component graph config, validate it, resolve backends.
- **Component graph**: Create push routes between components. Data flows automatically from source to destination — no state machine coordinates the pipeline.
- **Backend registry**: Named model implementations that the engine can build and route data to. Backends are registered at startup by the host (the Fluxer adapter, or a CLI, or tests).
- **Session lifecycle**: `start()` wires the graph, `stop()` tears it down. `feed_audio()` pushes PCM into the graph. `output_stream()` pulls PCM out. The session holds no state about the conversation — it's a data router.

The omni engine is the **inner core**. It knows about component graphs, push routes, backends, and data types. It does not know about Fluxer, Discord, Hermes tools, or user sessions — those are the plugin's job.

### 3. Local models

Local GGUF models, custom inference binaries, torch venvs, Vulkan ICD paths — these are environment-specific concerns. They belong in the host's startup configuration, not in the engine or the plugin. The engine exposes a `register_backend()` interface; the host (the Fluxer adapter, a test harness, a CLI) calls it with the models available on that machine. The engine doesn't care where the model runs or how it was built.

---

## The omni engine — how it works

### Profiles

A profile is the only config a user writes. It describes a component graph:

```yaml
foo-profile: # any name
  components:
    ears: # any name
      ins: { audio: [user] }
      outs: { text: [talker] }
      model: local.crispasr_stream
    talker:
      ins: { text: [ears, thinker] }
      outs: { text: [thinker] }
      tools: { defer: thinker }
      model: some-fast-text-model
    thinker:
      ins: { image: [user] }
      outs: { text: [talker] }
      tools: { harness: core, video: tape }
      model: default # hermes default
    mouth:
      ins: { text: [talker] }
      outs: { audio: [user] }
      model: local.kokoro
```

```

```
