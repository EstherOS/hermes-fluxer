"""End-to-end test: WAV → ASR → brain → TTS → binding capture.

Runs against the CURRENT sandbox setup:
- CrispASR CUDA (mini-omni2) for ASR
- llama-server at 8085 (MiniCPM5-2B) for brain
- Piper for TTS
"""

import asyncio
import sys
import time
import urllib.request
from pathlib import Path
from typing import AsyncGenerator

REPO = Path("/home/agent/workspace/fluxer")

sys.path.insert(0, str(REPO / "packages/omnimaker/src"))
sys.path.insert(0, str(REPO / "packages/omnimaker-hermes/src"))
sys.path.insert(0, str(REPO / "plugins/hermes-fluxer/src"))

from omnimaker import register_builtins
register_builtins()

from omnimaker.adapters.base import NodeAdapter
from omnimaker.adapters.registry import registry
from omnimaker.types import Envelope, ExecutionHandle

# Fluxer local backends
from fluxer.omni.backends import CrispAsrBackend, PiperTTSBackend
from fluxer.omni.backends_local import LlamaServerCompletion

registry.register("local/crispasr", CrispAsrBackend)
registry.register("local/piper", PiperTTSBackend)
registry.register("local/llama-completion", LlamaServerCompletion)


# Capture binding
class CaptureBinding(NodeAdapter):
    accepts = {"audio_out": ["audio"]}
    emits = {}
    mode = "duplex"
    session_scope = "persistent"

    def __init__(self):
        super().__init__()
        self.captured: list[bytes] = []

    async def open(self, session_id, config):
        self.session_id = session_id

    async def accept(self, env: Envelope, h: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        if isinstance(env.payload, bytes):
            self.captured.append(env.payload)
            print(f"  [BINDING] captured {len(env.payload)} bytes audio")
        if False:
            yield

registry.register_binding("fluxer", CaptureBinding)

HOME = Path.home()
CRISPASR = str(HOME / "workspace/fluxer-local/crispasr-cuda/crispasr")
CRISPASR_MODEL = str(HOME / "workspace/fluxer-local/models/omni/mini-omni2/mini-omni2-q4_k.gguf")
GPU_LIB = str(HOME / "workspace/fluxer-local/gpu-env/cuda-libs")
PIPER = str(HOME / "workspace/fluxer-local/gpu/tools/piper/piper")
PIPER_MODEL = str(HOME / "workspace/fluxer-local/models/en_US-lessac-medium.onnx")

from omnimaker.compiler import compile_profile
from omnimaker.runtime import Session
from omnimaker.types import Route, Endpoint


async def main():
    print("=" * 55)
    print("Pipeline: WAV → CrispASR → brain(8085) → Piper → binding")
    print("=" * 55)

    # Check llama-server is up
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8085/v1/chat/completions",
            data=b'{"messages":[{"role":"user","content":"hi"}],"max_tokens":5}',
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        print("llama-server (8085): up")
    except Exception as e:
        print(f"llama-server (8085): DOWN ({e})")
        return

    profile = compile_profile("hal9000-e2e", {
        "streams": {
            "mic": {"source": "@fluxer.audio"},
        },
        "nodes": {
            "ears": {
                "use": "local/crispasr", "mode": "turn_based",
                "consumes": ["mic"],
                "produces": ["transcript"],
                "config": {"binary": CRISPASR, "model": CRISPASR_MODEL,
                           "backend": "mini-omni2", "gpu_lib_path": GPU_LIB},
            },
            "brain": {
                "use": "local/llama-completion", "mode": "turn_based", "session": "persistent",
                "consumes": ["ears.transcript"],
                "produces": ["text"],
                "config": {"base_url": "http://127.0.0.1:8085", "max_tokens": 128,
                           "system_prompt": "You are HAL 9000. Reply in one short sentence."},
            },
            "mouth": {
                "use": "local/piper", "mode": "streaming",
                "consumes": ["brain.text"],
                "produces": ["audio"],
                "config": {"model": PIPER_MODEL, "binary": PIPER},
            },
        },
        "routes": [
            {"from": "ears.transcript", "to": "brain.input"},
            {"from": "brain.text", "to": "mouth.text"},
            {"from": "mouth.audio", "to": "@fluxer.audio_out"},
        ],
    })

    session = Session(profile)
    await session.start()

    binding = session.get_binding("fluxer")
    if binding:
        await binding.open(session.id, {})
        print(f"Binding attached: {type(binding).__name__}")

    # Load JFK WAV
    wav_path = HOME / "workspace/fluxer-local/models/jfk.wav"
    if not wav_path.exists():
        print(f"NO WAV at {wav_path}")
        await session.stop()
        return
    with open(wav_path, "rb") as f:
        wav_bytes = f.read()

    env = Envelope(type="audio", payload=wav_bytes, session_id=session.id,
                   turn_id="t1", execution_id="e1", source="@fluxer.audio")
    print(f"\nFeeding {len(wav_bytes)} bytes audio into graph...")
    t0 = time.monotonic()
    await session.feed("@fluxer.audio", env)
    # Give async pipeline time to complete
    for i in range(60):
        await asyncio.sleep(1)
        if binding.captured:
            break

    elapsed = time.monotonic() - t0
    print(f"Pipeline done in {elapsed:.1f}s")
    await session.stop()
    total = sum(len(c) for c in binding.captured)
    print(f"\nCaptured {len(binding.captured)} audio chunks ({total} bytes)")
    print("PASS" if binding.captured else "FAIL")


asyncio.run(main())