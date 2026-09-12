"""CrispASR streaming backend for hermes-omni — pipes PCM via stdin, reads JSON-Lines from stdout.

Spawns the CrispASR binary in streaming mode as a long-lived subprocess.
The binary reads raw s16le PCM from its stdin and emits JSON-Lines transcript
events to stdout.  This backend implements both the DuplexSession protocol
(for unified profiles) and the AudioIn.transcribe_stream protocol (for
stitched profiles).

Usage:
    backend = CrispAsrStreamingBackend()
    session = await backend.open_session()
    await session.open()
    await session.feed_audio(pcm_chunk)
    async for part in session.receive():
        print(part.text_of())

Or via the AudioIn protocol:
    async for text in session.transcribe_stream(chunk_stream()):
        print(text)

Environment: needs VK_DRIVER_FILES + LD_LIBRARY_PATH for the NVIDIA Vulkan ICD.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator

from ..types import BackendError, DuplexSession, Part

logger = logging.getLogger(__name__)


def _resolve_root() -> Path:
    return Path(os.environ.get("FLUXER_WORKSPACE", "/home/agent/workspace/fluxer-local"))


def _crispasr_binary(root: Path) -> Path:
    return root / "CrispASR/build/bin/crispasr"


def _model_path(root: Path) -> Path:
    return root / "models/omni/mini-omni2/mini-omni2-q4_k.gguf"


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


# ── streaming session ─────────────────────────────────────────────────────


class CrispAsrStreamingSession(DuplexSession):
    """Long-lived streaming session over the CrispASR binary.

    Spawns the binary with ``--stream --stream-json`` flags, pipes raw PCM
    chunks to its stdin, and reads JSON-Lines transcript events from stdout.

    Supports both the DuplexSession protocol (``send`` / ``receive``) and
    the AudioIn protocol (``transcribe_stream``).
    """

    def __init__(
        self,
        *,
        binary: str | Path | None = None,
        model: str | Path | None = None,
        root: str | Path | None = None,
        silence_ms: int = 800,
        step_ms: int = 3000,
    ) -> None:
        self._root = Path(root) if root is not None else _resolve_root()
        self._binary = Path(binary) if binary is not None else _crispasr_binary(self._root)
        self._model = Path(model) if model is not None else _model_path(self._root)
        self._silence_ms = silence_ms
        self._step_ms = step_ms

        self._proc: asyncio.subprocess.Process | None = None
        self._transcript_queue: asyncio.Queue[Part | None] = asyncio.Queue()
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False
        self._opened = False
        self._pre_buffer: list[bytes] = []

    # ── lifecycle ────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Start the CrispASR binary in streaming mode."""
        if self._opened:
            return
        self._opened = True

        cmd = [
            str(self._binary),
            "-m", str(self._model),
            "--backend", "mini-omni2",
            "--stream",
            "--stream-json",
            "--stream-final-on-silence-ms", str(self._silence_ms),
            "--stream-step", str(self._step_ms),
        ]

        env = _vulkan_env()
        logger.info("CrispAsrStreamingSession: spawning %s", " ".join(str(a) for a in cmd))

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            raise BackendError(
                f"CrispASR binary not found at {self._binary}. "
                "Build it from the CrispASR source tree first."
            )
        except Exception as exc:
            raise BackendError(f"Failed to spawn CrispASR: {exc}") from exc

        # Background reader tasks
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # Flush any chunks that were buffered before open() completed
        if self._pre_buffer:
            logger.debug(
                "CrispAsrStreamingSession: flushing %d pre-buffered chunk(s)",
                len(self._pre_buffer),
            )
            for chunk in self._pre_buffer:
                if self._proc.stdin is not None:
                    self._proc.stdin.write(chunk)
            if self._proc.stdin is not None:
                await self._proc.stdin.drain()
            self._pre_buffer.clear()

        logger.debug("CrispAsrStreamingSession opened (pid=%s)", self._proc.pid)

    async def _read_stdout(self) -> None:
        """Read JSON-Lines from stdout and push parsed events into the queue."""
        try:
            reader = self._proc.stdout
            if reader is None:
                return
            while True:
                line = await reader.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("CrispASR: non-JSON stdout line: %.80s", raw)
                    continue

                text = event.get("text", "")
                event_type = event.get("type", "partial")

                meta: dict[str, Any] = {
                    "type": event_type,
                    "confidence": event.get("confidence"),
                }
                if "start" in event:
                    meta["start"] = event["start"]
                if "end" in event:
                    meta["end"] = event["end"]

                part = Part.text(text, **meta)
                await self._transcript_queue.put(part)

                if event_type == "final":
                    logger.debug("CrispASR: final transcript: %.80s", text)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("CrispASR stdout reader error: %s", exc)
        finally:
            await self._transcript_queue.put(None)

    async def _read_stderr(self) -> None:
        """Log subprocess stderr at debug level."""
        try:
            reader = self._proc.stderr
            if reader is None:
                return
            while True:
                line = await reader.readline()
                if not line:
                    break
                logger.debug("CrispASR stderr: %s", line.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("CrispASR stderr reader stopped: %s", exc)
        finally:
            # Drain any remaining stderr on close
            pass

    # ── PCM input ────────────────────────────────────────────────────────

    async def feed_audio(self, chunk: bytes) -> None:
        """Write a raw PCM chunk (s16le, 16 kHz, mono) to the subprocess stdin.

        If the subprocess isn't ready yet, buffers the chunk — it will be
        flushed once ``open()`` completes.
        """
        if self._closed:
            raise BackendError("session is closed")
        if not self._opened or self._proc is None or self._proc.stdin is None:
            self._pre_buffer.append(chunk)
            return

        try:
            self._proc.stdin.write(chunk)
            await self._proc.stdin.drain()
        except BrokenPipeError:
            raise BackendError("CrispASR subprocess stdin closed (process may have crashed)")
        except Exception as exc:
            raise BackendError(f"Failed to write audio to CrispASR: {exc}") from exc

    # ── DuplexSession protocol ───────────────────────────────────────────

    async def send(self, part: Part) -> None:
        """Push one input part into the session.

        Audio parts are written to the subprocess stdin as raw PCM.
        Non-audio parts are silently ignored.
        """
        if self._closed or not self._opened:
            raise BackendError("session is closed or not opened")
        if part.kind != "audio":
            logger.debug("CrispAsrStreamingSession: ignoring non-audio part (kind=%s)", part.kind)
            return

        data = part.data
        if isinstance(data, str):
            path = Path(data)
            data = path.read_bytes()
        elif not isinstance(data, bytes):
            raise BackendError(f"unsupported audio data type: {type(data).__name__}")

        await self.feed_audio(data)

    def receive(self) -> AsyncIterator[Part]:
        """Async iterator over transcript parts from the subprocess.

        Yields ``Part.text`` objects with meta ``type`` = ``"partial"`` or
        ``"final"``.  The iterator ends when the session is closed.
        """

        async def _drain() -> AsyncIterator[Part]:
            while True:
                item = await self._transcript_queue.get()
                if item is None:
                    break
                yield item

        return _drain()

    # ── AudioIn protocol (transcribe_stream) ────────────────────────────

    async def transcribe_stream(
        self, chunk_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """Feed a stream of PCM chunks to the subprocess and yield transcript text.

        This implements the :class:`~hermes_omni.types.AudioIn.transcribe_stream`
        protocol.  The caller provides an async iterator of raw PCM chunks (s16le,
        16 kHz mono).  Partial transcripts are yielded as they arrive; the final
        transcript is the last yielded value.

        When the *chunk_stream* is exhausted, the subprocess stdin is closed to
        signal end-of-utterance.
        """
        if self._proc is None or self._proc.stdin is None:
            raise BackendError("session not opened; call open() first")

        # Background feeder: drain chunk_stream into subprocess stdin
        async def _feeder() -> None:
            try:
                async for chunk in chunk_stream:
                    try:
                        self._proc.stdin.write(chunk)
                        await self._proc.stdin.drain()
                    except BrokenPipeError:
                        break
            except asyncio.CancelledError:
                pass
            finally:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass

        feeder = asyncio.create_task(_feeder())

        try:
            last_text = ""
            last_type = "partial"
            while True:
                item = await self._transcript_queue.get()
                if item is None:
                    break
                if not item.is_text:
                    continue
                text = item.text_of()
                event_type = item.meta.get("type", "partial")
                if text:
                    last_text = text
                    last_type = event_type
                    yield text
                if event_type == "final":
                    break
            # If the stream ended without a final event, yield the last partial
            # as the final transcript.
            if last_text and last_type != "final":
                yield last_text
        finally:
            feeder.cancel()
            try:
                await feeder
            except (asyncio.CancelledError, Exception):
                pass

    # ── teardown ─────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Terminate the subprocess and clean up."""
        if self._closed:
            return
        self._closed = True

        if self._proc is not None:
            # Close stdin to signal end of stream
            if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass

            # Terminate with increasing force
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("CrispASR subprocess did not exit gracefully — killing")
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except Exception:
                    pass
            except Exception:
                pass

        # Cancel reader tasks
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Signal end to any receive() iterator
        await self._transcript_queue.put(None)
        logger.debug("CrispAsrStreamingSession closed")


# ── backend ───────────────────────────────────────────────────────────────


class CrispAsrStreamingBackend:
    """Streaming ASR backend for CrispASR with mini-omni2 GGUF (Vulkan GPU).

    Backend name: ``local.crispasr_stream``

    Opens long-lived subprocess sessions that pipe PCM directly to the
    CrispASR binary in streaming mode and read JSON-Lines transcript events
    from stdout.
    """

    name = "local.crispasr_stream"

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        binary: str | Path | None = None,
        model: str | Path | None = None,
        silence_ms: int = 800,
        step_ms: int = 3000,
        **kwargs: Any,
    ) -> None:
        self._root = Path(root) if root else _resolve_root()
        self._binary = Path(binary) if binary else _crispasr_binary(self._root)
        self._model = Path(model) if model else _model_path(self._root)
        self._silence_ms = silence_ms
        self._step_ms = step_ms

    async def open_session(self, **options: Any) -> CrispAsrStreamingSession:
        """Open a new streaming session (DuplexBackend protocol)."""
        return CrispAsrStreamingSession(
            binary=self._binary,
            model=self._model,
            root=self._root,
            silence_ms=self._silence_ms,
            step_ms=self._step_ms,
        )

    async def create_realtime(self, **options: Any) -> CrispAsrStreamingSession:
        """Open a streaming session for the unified profile's realtime backend.

        Returns the same session type as :meth:`open_session`.
        """
        return CrispAsrStreamingSession(
            binary=self._binary,
            model=self._model,
            root=self._root,
            silence_ms=self._silence_ms,
            step_ms=self._step_ms,
        )