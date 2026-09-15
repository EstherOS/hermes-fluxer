"""Fluxer node adapters — concrete backends for the omni engine."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import AsyncGenerator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.adapters.registry import registry
from omnimaker.types import Envelope, ExecutionHandle


# ── CrispASR Streaming ASR ─────────────────────────────────────────────

class CrispAsrStreamBackend(NodeAdapter):
    """Streaming ASR via CrispASR subprocess.

    Keeps a persistent subprocess running ``crispasr --stream --stream-json``,
    pipes PCM audio on stdin, reads JSON-Line transcripts from stdout.

    Config:
        binary: path to crispasr binary
        model: path to .gguf model file
        backend: CrispASR backend name (default "mini-omni2")
        stream_step_ms: partial emit interval (default 3000)
        final_silence_ms: silence threshold for final (default 800)
    """

    accepts = {"audio": ["audio"]}
    emits = {"transcript": ["transcript", "text"]}

    def __init__(self) -> None:
        super().__init__()
        self._proc: asyncio.subprocess.Process | None = None
        self._config: dict = {}
        self._session_id: str = ""

    async def open(self, session_id: str, config: dict) -> None:
        self._config = config
        self._session_id = session_id
        binary = config.get("binary", "crispasr")
        model = config.get("model", "")
        backend = config.get("backend", "mini-omni2")
        step = config.get("stream_step_ms", 3000)
        silence = config.get("final_silence_ms", 800)

        cmd = [
            binary, "-m", model,
            "--backend", backend,
            "--stream", "--stream-json",
            "--stream-step", str(step),
            "--stream-final-on-silence-ms", str(silence),
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        if not self._proc or not self._proc.stdin:
            return

        # Write PCM audio to stdin
        audio_data = envelope.payload
        if isinstance(audio_data, bytes):
            self._proc.stdin.write(audio_data)
            await self._proc.stdin.drain()

        # Read pending transcript lines from stdout
        if self._proc.stdout:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=1.0,
            )
            if line:
                try:
                    evt = json.loads(line.decode().strip())
                    text = evt.get("text", "")
                    is_final = evt.get("type") == "final"
                    yield Envelope(
                        type="transcript",
                        payload={"text": text, "final": is_final},
                        session_id=self._session_id,
                        turn_id=envelope.turn_id,
                        execution_id=handle.id,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

    async def cancel(self, execution_id: str) -> None:
        # Subprocess handles its own cancellation via stream reset
        pass

    async def close(self) -> None:
        if self._proc:
            self._proc.kill()
            await self._proc.wait()
            self._proc = None


# ── File-based CrispASR (half-duplex, per-utterance) ───────────────────

class CrispAsrBackend(NodeAdapter):
    """File-based ASR via CrispASR. Writes WAV, spawns process, reads transcript.

    Config:
        binary: path to crispasr binary
        model: path to .gguf model file
        backend: CrispASR backend name (default "mini-omni2")
        gpu_lib_path: dir containing CUDA libs (e.g. .../gpu-env/cuda-libs)
    """

    accepts = {"audio": ["audio"]}
    emits = {"transcript": ["text"]}

    def __init__(self) -> None:
        super().__init__()
        self._config: dict = {}
        self._tmp_dir: str = "/tmp"

    async def open(self, session_id: str, config: dict) -> None:
        self._config = config
        self._tmp_dir = config.get("tmp_dir", "/tmp")

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        import tempfile
        config = self._config
        binary = config.get("binary", "crispasr")
        model = config.get("model", "")
        backend = config.get("backend", "mini-omni2")
        gpu_lib_path = config.get("gpu_lib_path", "")
        audio_data = envelope.payload

        if not isinstance(audio_data, bytes):
            return

        with tempfile.NamedTemporaryFile(suffix=".wav", dir=self._tmp_dir,
                                         delete=False) as f:
            f.write(audio_data)
            wav_path = f.name

        try:
            # Build subprocess env with GPU lib paths if configured
            subprocess_env = None
            if gpu_lib_path:
                subprocess_env = dict(os.environ)
                cuda_path = os.path.join(gpu_lib_path)
                if os.path.isdir(cuda_path):
                    existing = subprocess_env.get("LD_LIBRARY_PATH", "")
                    subprocess_env["LD_LIBRARY_PATH"] = f"{cuda_path}:{existing}" if existing else cuda_path

            proc = await asyncio.create_subprocess_exec(
                binary, "-m", model, "--backend", backend,
                "-f", wav_path,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=subprocess_env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            text = stdout.decode().strip().rsplit("\n", 1)[-1]

            if text:
                yield Envelope(
                    type="text",
                    payload=text,
                    session_id=envelope.session_id,
                    turn_id=envelope.turn_id,
                    execution_id=handle.id,
                )
        finally:
            os.unlink(wav_path)

    async def close(self) -> None:
        pass


# ── Piper TTS ──────────────────────────────────────────────────────────

class PiperTTSBackend(NodeAdapter):
    """TTS via Piper. Produces PCM audio from text.

    Config:
        binary: path to piper binary (default "piper")
        model: path to Piper model
        voice: Piper voice name (default "en_US-amy-medium")
        sample_rate: output sample rate (default 22050)
    """

    accepts = {"text": ["text"]}
    emits = {"audio": ["audio"]}

    def __init__(self) -> None:
        super().__init__()
        self._config: dict = {}

    async def open(self, session_id: str, config: dict) -> None:
        self._config = config

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        text = envelope.payload
        if not isinstance(text, str):
            return

        binary = self._config.get("binary", "piper")
        model_path = self._config.get("model", "")
        if not model_path:
            return

        proc = await asyncio.create_subprocess_exec(
            binary, "--model", model_path,
            "--output-raw",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, _ = await proc.communicate(input=text.encode())
        except asyncio.CancelledError:
            proc.kill()
            raise

        if stdout:
            yield Envelope(
                type="audio",
                payload=stdout,
                session_id=envelope.session_id,
                turn_id=envelope.turn_id,
                execution_id=handle.id,
            )

    async def close(self) -> None:
        pass


# ── Kokoro TTS (via CrispASR) ──────────────────────────────────────────

class KokoroTTSBackend(NodeAdapter):
    """TTS via Kokoro compiled into CrispASR binary.

    Config:
        binary: path to crispasr binary (default "crispasr")
        output_sample_rate: (default 24000)
    """

    accepts = {"text": ["text"]}
    emits = {"audio": ["audio"]}

    def __init__(self) -> None:
        super().__init__()
        self._config: dict = {}
        self._tmp_dir: str = "/tmp"

    async def open(self, session_id: str, config: dict) -> None:
        self._config = config
        self._tmp_dir = config.get("tmp_dir", "/tmp")

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        import tempfile
        text = envelope.payload
        if not isinstance(text, str):
            return

        binary = self._config.get("binary", "crispasr")
        sample_rate = self._config.get("output_sample_rate", 24000)

        with tempfile.NamedTemporaryFile(suffix=".wav", dir=self._tmp_dir,
                                         delete=False) as f:
            out_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "--backend", "kokoro", "-m", "auto",
                "--tts", text, "--tts-output", out_path,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)

            with open(out_path, "rb") as f:
                wav_bytes = f.read()

            if wav_bytes:
                yield Envelope(
                    type="audio",
                    payload=wav_bytes,
                    session_id=envelope.session_id,
                    turn_id=envelope.turn_id,
                    execution_id=handle.id,
                )
        finally:
            os.unlink(out_path)

    async def close(self) -> None:
        pass


# ── Registration function ──────────────────────────────────────────────

def register_fluxer_backends() -> None:
    """Register all fluxer-provided node adapters."""
    from fluxer.omni.binding import FluxerBinding
    from fluxer.omni.backends_llm import LLMCompletionAdapter

    registry.register("local/crispasr", CrispAsrBackend)
    registry.register("local/crispasr_stream", CrispAsrStreamBackend)
    registry.register("local/piper", PiperTTSBackend)
    registry.register("local/kokoro", KokoroTTSBackend)
    registry.register("local/llm-completion", LLMCompletionAdapter)
    registry.register_binding("fluxer", FluxerBinding)