"""CrispASR Mini-Omni2 backend for hermes-omni — wraps the Vulkan-accelerated binary.

Runs the locally-built CrispASR binary (ggml, Vulkan GPU) with the mini-omni2
GGUF model for speech-to-speech and text-to-speech.  The binary was built from
source at *FLUXER_WORKSPACE* /CrispASR/build/bin/crispasr.

VRAM: ~1 GiB at Q4_K, 48 MiB KV cache.
Performance: 3.3× realtime ASR on GTX 1060 Vulkan.
Environment: needs VK_DRIVER_FILES + LD_LIBRARY_PATH for the NVIDIA Vulkan ICD
(see gpu/gpu-env.sh).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator

from ..types import BackendError, DuplexSession, Part

logger = logging.getLogger(__name__)


def _resolve_root() -> Path:
    return Path(os.environ.get("FLUXER_WORKSPACE", "/home/agent/workspace/fluxer-local"))


def _crispasr_binary(root: Path) -> Path:
    return root / "CrispASR/build/bin/crispasr"


def _vulkan_env() -> dict[str, str]:
    """Return environment dict with NVIDIA Vulkan ICD paths."""
    env = dict(os.environ)
    root = _resolve_root()
    icd = str(root / "gpu/nvidia_icd_egl.json")
    egl_lib = str(root / "gpu/extract-egl/usr/lib/x86_64-linux-gnu")
    if os.path.isfile(icd):
        env.setdefault("VK_DRIVER_FILES", icd)
    if os.path.isdir(egl_lib):
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{egl_lib}:{existing}" if existing else egl_lib
    return env


class CrispAsrMiniOmni2Session(DuplexSession):
    """One half-duplex turn over CrispASR + mini-omni2 GGUF.

    ``send(part)`` runs ASR (audio → text) via the CrispASR binary and
    places the transcript into the output queue.  ``receive()`` yields
    the text part.
    """

    def __init__(
        self,
        *,
        binary: str | Path | None = None,
        model: str | Path | None = None,
        root: str | Path | None = None,
        max_tokens: int = 2048,
        timeout: float = 120.0,
    ) -> None:
        self._root = Path(root) if root is not None else _resolve_root()
        self._binary = Path(binary) if binary is not None else _crispasr_binary(self._root)
        self._model = Path(model) if model is not None else (
            self._root / "models/omni/mini-omni2/mini-omni2-q4_k.gguf"
        )
        self._snac = self._root / "models/omni/mini-omni2/snac-24khz.gguf"
        self._timeout = timeout
        self._queue: asyncio.Queue[Part | None] = asyncio.Queue()
        self._closed = False
        self._opened = False

    async def open(self) -> None:
        self._opened = True
        logger.debug("CrispAsrSession opened")

    async def send(self, part: Part) -> None:
        if self._closed or not self._opened:
            raise BackendError("session is closed or not opened")
        if part.kind != "audio":
            logger.debug("CrispAsrSession: ignoring non-audio part (kind=%s)", part.kind)
            return

        loop = asyncio.get_running_loop()
        audio_path = self._write_audio(part)
        try:
            cmd = [
                str(self._binary),
                "-m", str(self._model),
                "-f", audio_path,
                "--backend", "mini-omni2",
            ]

            env = _vulkan_env()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=self._timeout, env=env,
                ),
            )

            if proc.returncode != 0:
                stderr = proc.stderr.strip()[-500:]
                raise BackendError(f"CrispASR rc={proc.returncode}: {stderr}")

            # Extract transcript from stdout — the last non-empty line
            lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
            for line in reversed(lines):
                if line.startswith("crispasr: transcribed") or line.startswith("crispasr[lid]:"):
                    continue
                # The transcript is the final non-header line
                transcript = line
                await self._queue.put(Part.text(transcript))
                logger.info("CrispAsrSession: transcript: %.80s", transcript)
                return

            raise BackendError("CrispASR produced no transcript")

        except subprocess.TimeoutExpired:
            raise BackendError(f"CrispASR timed out after {self._timeout}s")
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"CrispASR failed: {exc}") from exc
        finally:
            if os.path.isfile(audio_path) and audio_path.startswith(tempfile.gettempdir()):
                os.unlink(audio_path)

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
        data = part.data
        if isinstance(data, str):
            return data
        tmp = tempfile.mktemp(suffix=".wav", prefix="hermes-omni-crispasr-")
        with open(tmp, "wb") as f:
            f.write(data if isinstance(data, bytes) else str(data).encode())
        return tmp


class CrispAsrMiniOmni2Backend:
    """DuplexBackend for CrispASR + mini-omni2 GGUF (Vulkan GPU).

    Backend name: ``local.crispasr_miniomni2``
    Built from source at CrispASR/build/bin/crispasr with Vulkan support.
    """

    name = "local.crispasr_miniomni2"

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        binary: str | Path | None = None,
        model: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        self._root = Path(root) if root else _resolve_root()
        self._binary = Path(binary) if binary else _crispasr_binary(self._root)
        self._model = Path(model) if model else (
            self._root / "models/omni/mini-omni2/mini-omni2-q4_k.gguf"
        )

    async def open_session(self, **options: Any) -> DuplexSession:
        return CrispAsrMiniOmni2Session(
            binary=self._binary, model=self._model, root=self._root,
        )