"""End-to-end HAL 9000 pipeline — CUDA-accelerated."""

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

# Bootstrap fluxer backends
spec = importlib.util.spec_from_file_location("b1", "plugins/hermes-fluxer/src/fluxer/omni/backends.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["b1"] = mod
spec.loader.exec_module(mod)
registry.register("local/crispasr", mod.CrispAsrBackend)
registry.register("local/piper", mod.PiperTTSBackend)

# Echo brain
class EchoBrain(NodeAdapter):
    accepts = {"in": ["text", "transcript"]}; emits = {"out": ["text"]}
    mode = "turn_based"; emits_style = "final_only"; session_scope = "persistent"
    async def accept(self, env: Envelope, h: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        text = env.payload if isinstance(env.payload, str) else env.payload.get("text", "")
        if text:
            print(f"  [BRAIN] echo: '{text[:50]}'")
            yield Envelope(type="text", payload=text, session_id=env.session_id, turn_id=env.turn_id, execution_id=h.id)
        if False: yield
    pass

# Capture
class Capture(NodeAdapter):
    accepts = {"in": ["audio", "text", "transcript"]}; emits = {}
    mode = "streaming"
    def __init__(self): super().__init__(); self.captured = []
    async def accept(self, env: Envelope, h: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        self.captured.append(env)
        print(f"  [CAPTURE #{len(self.captured)}] type={env.type} len={len(env.payload) if isinstance(env.payload, bytes) else 0}")
        if False: yield

registry.register("test/echo-brain", EchoBrain)
registry.register("test/capture", Capture)

HOME = Path.home()
CRISPASR_BIN = str(HOME / "workspace/fluxer-local/crispasr-cuda/crispasr")
CRISPASR_MODEL = str(HOME / "workspace/fluxer-local/models/omni/mini-omni2/mini-omni2-q4_k.gguf")
GPU_LIB_PATH = str(HOME / "workspace/fluxer-local/gpu-env/cuda-libs")
PIPER = str(HOME / "workspace/fluxer-local/gpu/tools/piper/piper")
PIPER_MODEL = str(HOME / "workspace/fluxer-local/models/en_US-lessac-medium.onnx")
WAV = HOME / "workspace/fluxer-local/models/jfk.wav"

for p in [CRISPASR_BIN, CRISPASR_MODEL, PIPER, str(PIPER_MODEL), str(WAV)]:
    assert os.path.exists(p), f"Missing: {p}"

from hermes_omni.compiler import compile_profile
from hermes_omni.runtime import Session

async def main():
    print("=" * 50)
    print("HAL 9000 — CUDA pipeline test")
    print("=" * 50)

    profile = compile_profile("hal9000", {
        "nodes": {
            "ears": {
                "use": "local/crispasr", "mode": "turn_based", "emits": "final_only",
                "produces": ["transcript"],
                "config": {"binary": CRISPASR_BIN, "model": CRISPASR_MODEL,
                           "backend": "mini-omni2", "gpu_lib_path": GPU_LIB_PATH},
            },
            "brain": {
                "use": "test/echo-brain", "mode": "turn_based", "session": "persistent",
                "produces": ["text"],
            },
            "mouth": {
                "use": "local/piper", "mode": "streaming",
                "produces": ["audio"],
                "config": {"model": PIPER_MODEL, "binary": PIPER},
            },
            "capture": {
                "use": "test/capture", "mode": "streaming",
                "produces": [],
            },
        },
        "routes": [
            {"from": "ears.input", "to": "ears.input"},
            {"from": "ears.transcript", "to": "brain.input"},
            {"from": "brain.text", "to": "mouth.text"},
            {"from": "mouth.audio", "to": "capture.in"},
        ],
        "outputs": {},
    })

    print(f"Nodes: {list(profile.nodes.keys())}")
    for r in profile.routes:
        print(f"  {r.source} -> {r.dest}")

    session = Session(profile)
    await session.start()
    print(f"\nSession {session.id} started")

    with open(WAV, "rb") as f:
        wav_bytes = f.read()

    env = Envelope(type="audio", payload=wav_bytes, session_id=session.id,
                   turn_id="t1", execution_id="e1", source="ears.input")
    print(f"Feeding {len(wav_bytes)} bytes...")
    t0 = time.monotonic()
    await session.feed("ears.input", env)
    elapsed = time.monotonic() - t0
    print(f"Pipeline completed in {elapsed:.1f}s")

    await session.stop()

    cap = session._adapters.get("capture")
    if cap and cap.captured:
        audio = cap.captured[0].payload
        print(f"\nRESULT: {len(audio)} bytes TTS audio")
        assert isinstance(audio, bytes) and len(audio) > 1000
        print("PASS")
    else:
        print("\nFAIL: no audio captured")

if __name__ == "__main__":
    asyncio.run(main())