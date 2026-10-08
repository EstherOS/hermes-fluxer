# Hermes-Fluxer — Fluxer voice platform for Omnimaker

This plugin integrates Omnimaker with the Fluxer voice platform. It provides platform bindings (`@fluxer.*`) and local backends for ASR and TTS.

## Components

- **Binding** (`@fluxer.audio`, `@fluxer.audio_out`, `@fluxer.speech`) — routes audio envelopes between the omni graph and LiveKit voice rooms; `@fluxer.speech` carries the speech-onset signal for barge wiring
- **Backends** — `local/crispasr` (file-based ASR), `local/crispasr_stream` (streaming ASR), `local/piper` (TTS), `local/kokoro` (TTS), plus completion helpers
- **Voice bridge** — VAD-based audio capture, utterance detection, speech-onset signalling, injection into the omni graph

## Profile example

```yaml
nodes:
  ears:
    use: local/crispasr
    session: ephemeral
    consumes: [mic]
    produces: [transcript]
    config:
      binary: /path/to/crispasr
      model: /path/to/model.gguf
      backend: mini-omni2

  brain:
    use: hermes/session
    session: persistent
    consumes: [ears.transcript]
    produces: [text]
    concurrency:
      on_interrupt: replace     # a new utterance supersedes the running answer
    config:
      base_url: http://127.0.0.1:8085
      model: minicpm5-2b
      identity: false

  mouth:
    use: local/piper
    session: ephemeral
    consumes: [brain.text]
    produces: [audio]
    config:
      model: /path/to/model.onnx
      binary: /path/to/piper

routes:
  - from: ears.transcript
    to: brain.input
  - from: brain.text
    to: mouth.text
  - from: mouth.audio
    to: "@fluxer.audio_out"
  - from: "@fluxer.speech"      # speech onset (VAD) — barge wiring
    to: brain.cancel
  - from: "@fluxer.speech"
    to: mouth.cancel
```

Speech onset arrives as an envelope from `@fluxer.speech` (emitted by the bridge when the VAD detects sustained speech, ~300 ms). Routing it at `*.cancel` control endpoints is the entire barge-in wiring — the bridge also flushes its own playback queue and aborts the in-flight segment so the speaker goes quiet immediately.