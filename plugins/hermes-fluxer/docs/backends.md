# Backends

The fluxer plugin registers node adapters for local ASR and TTS at plugin load: `local/crispasr`, `local/crispasr_stream`, `local/piper`, `local/kokoro`, plus the completion helpers `local/llm-completion` and `local/llama-completion` (each registered when its dependencies are importable). The `hermes/session` adapter from the omnimaker-hermes package is registered by the same mixin.

## `local/crispasr`

File-based ASR. Takes an utterance WAV, returns a transcript. Half-duplex; suited to turn-based profiles.

Config: `binary`, `model`, `backend`, `tmp_dir`, `gpu_lib_path`.

## `local/crispasr_stream`

Streaming ASR via a persistent subprocess: reads raw PCM, emits JSON-Line events (partial and final). Suited to realtime profiles that need partials.

Config: `binary`, `model`, `backend`, `stream_step_ms`, `final_silence_ms`.

## `local/piper`

TTS via Piper. Produces raw audio from text.

Config: `binary`, `model` (voice model path).

## `local/kokoro`

TTS via Kokoro (compiled into CrispASR).

Config: `binary`, `output_sample_rate`, `tmp_dir`.

## `local/llm-completion` / `local/llama-completion`

Single-call chat completion helpers — a remote OpenAI-compatible endpoint and a local llama-server respectively. Each keeps conversation state for its session and emits one text envelope per turn.

Config: `system_prompt`, plus the endpoint and model keys the respective client reads.

## Adding a backend

Implement the adapter interface in one of the plugin's `omni/` modules and register it:

```python
from omnimaker import register_adapter
from omnimaker.adapters import NodeAdapter

class MyBackend(NodeAdapter):
    accepts = {"input": ["text"]}
    emits = {"output": ["audio"]}

    async def accept(self, envelope, handle):
        ...   # yield output envelopes

register_adapter("local/my_backend", MyBackend)
```

The engine handles routing, concurrency, and cancellation; the backend implements `accept()`, plus `cancel()`/`close()` when it holds resources.
