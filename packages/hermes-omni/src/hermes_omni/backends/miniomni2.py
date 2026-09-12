"""Mini-Omni2 DuplexBackend for hermes-omni — wraps the C7 torch path.

This backend bridges the file-based Mini-Omni2 inference (torch 2.3.1, Python 3.10)
into the hermes-omni protocol so the engine can use it as a unified realtime model.

Architecture:
- The torch stack lives in its own Python 3.10 venv at FLUXER_WORKSPACE/.venv-torch/
- The repo checkout is at FLUXER_WORKSPACE/mini-omni2/
- We shell out to the venv python with the probe script (scripts/miniomni2_probe.py)
- This is half-duplex (file in, file out) because the upstream inference code is
  half-duplex — but it's GPU-accelerated (CUDA on Pascal, ~30 tok/s).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from ..types import (
    BackendError,
    DuplexSession,
    Part,
    SenseBinding,
)

logger = logging.getLogger(__name__)


def _resolve_root() -> Path:
    return Path(os.environ.get("FLUXER_WORKSPACE", "/home/agent/workspace/fluxer-local"))


def _resolve_venv(root: Path) -> Path:
    return root / ".venv-torch/bin/python"


def _resolve_script() -> Path:
    # Probe script lives in the fluxer project tree (monorepo root)
    return Path("/home/agent/workspace/fluxer/scripts/miniomni2_probe.py")


class MiniOmni2Session(DuplexSession):
    """One half-duplex turn session over the torch Mini-Omni2 pipeline.

    ``send(part)`` runs inference with the given input (audio or image+audio)
    and buffers output.  ``receive()`` yields the output parts (text + audio).
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.9,
    ) -> None:
        self._root = root or _resolve_root()
        self._venv_python = _resolve_venv(self._root)
        self._probe_script = _resolve_script()
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._queue: asyncio.Queue[Part | None] = asyncio.Queue()
        self._closed = False
        self._opened = False
        self._sent = 0

    async def open(self) -> None:
        self._opened = True
        logger.debug("MiniOmni2Session opened")

    async def send(self, part: Part) -> None:
        if self._closed or not self._opened:
            raise BackendError("session is closed or not opened")
        self._sent += 1
        loop = asyncio.get_running_loop()
        out_wav = tempfile.mktemp(suffix=".wav", prefix="hermes-omni-mo2-")
        try:
            # Build CLI args
            cmd = [
                str(self._venv_python),
                str(self._probe_script),
                "--audio", str(self._write_audio(part)),
                "--out", out_wav,
                "--max-tokens", str(self._max_tokens),
                "--temperature", str(self._temperature),
            ]
            if part.kind == "image":
                cmd.extend(["--image", self._write_image(part)])

            # Run inference (blocking subprocess, wrapped in thread)
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=self._root,
                ),
            )

            if proc.returncode != 0:
                stderr = proc.stderr.strip()[-500:]
                raise BackendError(
                    f"Mini-Omni2 probe rc={proc.returncode}: {stderr}"
                )

            # Parse PROBE_JSON from stdout
            for line in proc.stdout.splitlines():
                if line.startswith("PROBE_JSON "):
                    import json
                    metrics = json.loads(line[11:])
                    break
            else:
                raise BackendError("Mini-Omni2 probe did not output PROBE_JSON")

            # Yield output text part if present
            text = metrics.get("text") or ""
            if text.strip():
                await self._queue.put(Part.text(text.strip()))

            # Yield output audio part if WAV was written
            if os.path.isfile(out_wav) and os.path.getsize(out_wav) > 100:
                with open(out_wav, "rb") as f:
                    wav_bytes = f.read()
                await self._queue.put(
                    Part.audio(wav_bytes, mime="audio/wav", meta={"sr": 24000})
                )

        except subprocess.TimeoutExpired:
            raise BackendError("Mini-Omni2 inference timed out (>300s)")
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"Mini-Omni2 inference failed: {exc}") from exc
        finally:
            for tmp in [out_wav]:
                if os.path.isfile(tmp):
                    os.unlink(tmp)

    def receive(self) -> AsyncIterator[Part]:
        async def _drain() -> AsyncIterator[Part]:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                yield item
        return _drain()

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)

    @staticmethod
    def _write_audio(part: Part) -> str:
        """Write part audio data to a temp WAV file and return the path."""
        data = part.data
        if isinstance(data, str):
            return data  # already a path
        import tempfile
        tmp = tempfile.mktemp(suffix=".wav", prefix="hermes-omni-mo2-audio-")
        with open(tmp, "wb") as f:
            f.write(data if isinstance(data, bytes) else str(data).encode())
        return tmp

    @staticmethod
    def _write_image(part: Part) -> str:
        """Write part image data to a temp file and return the path."""
        data = part.data
        if isinstance(data, str):
            return data
        ext = ".jpg"
        if part.mime:
            if "png" in part.mime:
                ext = ".png"
            elif "webp" in part.mime:
                ext = ".webp"
        tmp = tempfile.mktemp(suffix=ext, prefix="hermes-omni-mo2-image-")
        with open(tmp, "wb") as f:
            f.write(data if isinstance(data, bytes) else str(data).encode())
        return tmp


class MiniOmni2DuplexBackend:
    """DuplexBackend for the torch Mini-Omni2 pipeline.

    Implements the old-style registry backend interface (buildable via
    ``get_backend``) as well as the ``DuplexBackend`` protocol so it can
    be used in a ``mode: unified`` profile.

    Backend name: ``local.miniomni2_duplex``
    """

    name = "local.miniomni2_duplex"

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.9,
        **kwargs: Any,
    ) -> None:
        self._root = Path(root) if root else _resolve_root()
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def open_session(self, **options: Any) -> DuplexSession:
        return MiniOmni2Session(
            root=self._root,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )