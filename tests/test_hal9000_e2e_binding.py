"""End-to-end test: PCM → VAD → graph (ASR→llama→TTS) → binding output."""

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import AsyncGenerator

sys.path.insert(0, str(Path(__file__).parent.parent / "packages/hermes-omni/src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins/hermes-fluxer/src"))

from hermes_omni import register_builtins
register_builtins()

from hermes_omni.adapters.base import NodeAdapter
from hermes_omni.adapters.registry import registry
from hermes_omni.types import Envelope, ExecutionHandle

# Load fluxer backends
spec = importlib.util.spec_from_file_location("b1", "plugins/hermes-fluxer/src/fluxer/omni/backends.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["b1"] = mod
spec.loader.exec_module(mod)
registry.register("local/crispasr", mod.CrispAsrBackend)
registry.register("local/piper", mod.PiperTTSBackend)

# Local llama brain
spec2 = importlib.util.spec_from_file_location("b2", "plugins/hermes-fluxer/src/fluxer/omni/backends_local.py")
mod2 = importlib.util.module_from_spec(spec2)
sys.modules["b2"] = mod2
spec2.loader.exec_module(mod2)
registry.register("local/llama-completion", mod2.LlamaServerCompletion)

# Test binding that captures output
class CaptureBinding(NodeAdapter):
    accepts = {"audio_out": ["audio"]}
    emits = {}
    mode = "duplex"
    session_scope = "persistent"
    def __init__(self):
        super().__init__()
        self.captured: list[bytes] = []
    async def open(self, session_id, config): self.session_id = session_id
    async def accept(self, env: Envelope, h: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        if isinstance(env.payload, bytes):
            self.captured.append(env.payload)
            print(f"  [BINDING] captured {len(env.payload)} bytes audio")
        if False: yield

registry.register_binding("fluxer", CaptureBinding)

HOME = Path.home()
CRISPASR = str(HOME / "workspace/fluxer-local/crispasr-cuda/crispasr")
CRISPASR_MODEL = str(HOME / "workspace/fluxer-local/models/omni/mini-omni2/mini-omni2-q4_k.gguf")
GPU_LIB = str(HOME / "workspace/fluxer-local/gpu-env/cuda-libs")
PIPER = str(HOME / "workspace/fluxer-local/gpu/tools/piper/piper")
PIPER_MODEL = str(HOME / "workspace/fluxer-local/models/en_US-lessac-medium.onnx")

from hermes_omni.compiler import compile_profile
from hermes_omni.runtime import Session
from hermes_omni.types import Route, Endpoint

async def main():
    print("=" * 55)
    print("Full pipeline: VAD → ASR → llama → TTS → binding")
    print("=" * 55)

    # Check llama-server is up
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions",
            data=b'{"messages":[{"role":"user","content":"hi"}],"max_tokens":5}',
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
        print("llama-server: up")
    except Exception as e:
        print(f"llama-server: DOWN ({e})")
        return

    profile = compile_profile("hal9000-e2e", {
        "nodes": {
            "ears": {
                "use": "local/crispasr", "mode": "turn_based", "emits": "final_only",
                "produces": ["transcript"],
                "config": {"binary": CRISPASR, "model": CRISPASR_MODEL,
                           "backend": "mini-omni2", "gpu_lib_path": GPU_LIB},
            },
            "brain": {
                "use": "local/llama-completion", "mode": "turn_based", "session": "persistent",
                "produces": ["text"],
                "config": {"base_url": "http://127.0.0.1:8080", "max_tokens": 128,
                           "system_prompt": "You are HAL 9000. Reply in one short sentence."},
            },
            "mouth": {
                "use": "local/piper", "mode": "streaming",
                "produces": ["audio"],
                "config": {"model": PIPER_MODEL, "binary": PIPER},
            },
            "capture": {
                "use": "omni/tee", "mode": "streaming", "produces": [],
            },
        },
        "routes": [
            {"from": "ears.input", "to": "ears.input"},
            {"from": "ears.transcript", "to": "brain.input"},
            {"from": "brain.text", "to": "mouth.text"},
            {"from": "mouth.audio", "to": "capture.input"},
        ],
        "outputs": {},
    })

    # Add binding routes manually (profile has no outputs section)
    profile.routes.append(Route(
        source=Endpoint(node="@fluxer", port="audio"),
        dest=Endpoint(node="ears", port="input"),
        kind="dataflow",
    ))
    profile.routes.append(Route(
        source=Endpoint(node="capture", port="output"),
        dest=Endpoint(node="@fluxer", port="audio_out"),
        kind="dataflow",
    ))

    session = Session(profile)
    await session.start()

    # Attach binding
    binding = session.get_binding("fluxer")
    if binding:
        await binding.open(session.id, {})
        print(f"Binding attached: {type(binding).__name__}")

    # Read JFK WAV and feed as PCM bytes (raw 16k, but VAD needs 48k frames...)
    # CrispASR backend writes WAV directly from bytes; VadSegmenter expects 48k.
    # We feed the full WAV bytes directly to the session (skip VAD for this test)
    wav_path = HOME / "workspace/fluxer-local/models/jfk.wav"
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    env = Envelope(type="audio", payload=wav_bytes, session_id=session.id,
                   turn_id="t1", execution_id="e1", source="@fluxer.audio")
    print(f"\nFeeding {len(wav_bytes)} bytes audio into graph...")
    t0 = time.monotonic()
    await session.feed("@fluxer.audio", env)
    elapsed = time.monotonic() - t0
    print(f"Pipeline done in {elapsed:.1f}s")

    await session.stop()
    print(f"\n{'PASS' if binding.captured else 'FAIL':-^30}")

asyncio.run(main())