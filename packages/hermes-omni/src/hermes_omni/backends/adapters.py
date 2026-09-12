"""Protocol adapter wrappers for the omni engine (spec §6, wave 4).

Each adapter wraps a :class:`~hermes_omni.types.SenseBackend` that implements the
old ``process(part)`` interface and exposes it as one of the new role-specific
protocols (:class:`~hermes_omni.types.AudioIn`,
:class:`~hermes_omni.types.AudioOut`,
:class:`~hermes_omni.types.Vision`,
:class:`~hermes_omni.types.Text`).

This lets the existing backends (whispercpp, piper, qwen3tts, qwen3asr,
smolvlm2, llama-server) be used anywhere the new protocol types are expected
without modifying the backends themselves.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from ..types import (
    AudioIn,
    AudioOut,
    BackendError,
    Part,
    SenseBackend,
    Text,
    UncancelableError,
    Vision,
)

__all__ = [
    "AudioInFromSenseBackend",
    "AudioOutFromSenseBackend",
    "VisionFromSenseBackend",
    "TextFromSenseBackend",
]


# ── helpers ────────────────────────────────────────────────────────────────────


def _unwrap_text(results: Sequence[Part], *, what: str) -> str:
    """Extract text from a sequence of parts; raise if none is text."""
    for p in results:
        if p.is_text:
            return p.text_of()
    raise BackendError(
        f"{what}: backend returned no text part "
        f"(got {[p.kind for p in results]})"
    )


def _unwrap_audio(results: Sequence[Part], *, what: str) -> bytes:
    """Extract audio bytes from a sequence of parts."""
    for p in results:
        if p.kind == "audio":
            data = p.data
            if isinstance(data, bytes):
                return data
            if isinstance(data, (bytearray, memoryview)):
                return bytes(data)
            if isinstance(data, Path):
                return data.read_bytes()
            if isinstance(data, str):
                return Path(data).read_bytes()
            raise BackendError(
                f"{what}: unexpected audio data type {type(data).__name__}"
            )
    raise BackendError(
        f"{what}: backend returned no audio part "
        f"(got {[p.kind for p in results]})"
    )


async def _run_backend(backend: SenseBackend, part: Part) -> Sequence[Part]:
    """Call ``backend.process(part)`` and await if async."""
    result = backend.process(part)
    if isinstance(result, asyncio.Future) or asyncio.iscoroutine(result):
        result = await result
    if hasattr(result, "__aiter__"):
        return [p async for p in result]
    return list(result) if hasattr(result, "__iter__") else [result]


# ── AudioIn adapter ────────────────────────────────────────────────────────────


class AudioInFromSenseBackend(AudioIn):
    """Wrap a :class:`~hermes_omni.types.SenseBackend` serving ``audio_in``
    as an :class:`~hermes_omni.types.AudioIn` protocol.

    The wrapped backend must accept :class:`Part.audio` and return text
    :class:`Part` objects (whispercpp, qwen3asr, llama-server with
    ``input_audio`` transport).
    """

    def __init__(
        self, backend: SenseBackend, *, sample_rate: int = 16000
    ) -> None:
        self._backend = backend
        self._sample_rate = sample_rate

    async def transcribe(
        self, wav_bytes: bytes, *, sample_rate: int = 16000
    ) -> str:
        """Wrap *wav_bytes* in :class:`Part.audio`, call the wrapped
        backend's ``process``, and return the transcript text."""
        part = Part.audio(wav_bytes, meta={"sample_rate": sample_rate})
        results = await _run_backend(self._backend, part)
        return _unwrap_text(results, what="AudioInFromSenseBackend.transcribe")

    def transcribe_stream(
        self, chunk_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[str]:
        """Accumulate PCM chunks into a ``wav`` buffer, then transcribe
        the complete utterance once the stream ends.

        For true streaming ASR the backend should implement the
        :class:`AudioIn` protocol directly; this simple adapter collects
        chunks until the stream finishes.
        """

        async def _stream() -> AsyncIterator[str]:
            buffer = bytearray()
            async for chunk in chunk_stream:
                buffer.extend(chunk)
            if buffer:
                yield await self.transcribe(bytes(buffer))

        return _stream()

    async def cancel(self) -> None:
        """Delegate cancellation to the wrapped backend if it supports it."""
        cancel = getattr(self._backend, "cancel", None)
        if cancel is not None:
            result = cancel()
            if asyncio.iscoroutine(result):
                await result
            return
        raise UncancelableError(
            f"{type(self._backend).__name__} does not support cancellation"
        )


# ── AudioOut adapter ───────────────────────────────────────────────────────────


class AudioOutFromSenseBackend(AudioOut):
    """Wrap a :class:`~hermes_omni.types.SenseBackend` serving ``audio_out``
    as an :class:`~hermes_omni.types.AudioOut` protocol.

    The wrapped backend must accept :class:`Part.text` and return audio
    :class:`Part` objects (piper, qwen3tts).
    """

    def __init__(self, backend: SenseBackend) -> None:
        self._backend = backend

    async def synthesize(
        self, text: str, *, voice: str | None = None
    ) -> bytes:
        """Wrap *text* in :class:`Part.text`, call the wrapped backend's
        ``process``, and return the audio bytes."""
        meta: dict[str, Any] = {}
        if voice is not None:
            meta["voice"] = voice
        part = Part.text(text, **meta)
        results = await _run_backend(self._backend, part)
        return _unwrap_audio(
            results, what="AudioOutFromSenseBackend.synthesize"
        )

    def synthesize_stream(
        self,
        text_stream: AsyncIterator[str],
        *,
        voice: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Synthesize each text chunk as it arrives, yielding audio
        per chunk.

        For true streaming TTS the backend should implement the
        :class:`AudioOut` protocol directly; this simple adapter yields
        one complete WAV per text chunk.
        """

        async def _stream() -> AsyncIterator[bytes]:
            async for text in text_stream:
                yield await self.synthesize(text, voice=voice)

        return _stream()

    async def cancel(self) -> None:
        """Delegate cancellation to the wrapped backend if it supports it."""
        cancel = getattr(self._backend, "cancel", None)
        if cancel is not None:
            result = cancel()
            if asyncio.iscoroutine(result):
                await result
            return
        raise UncancelableError(
            f"{type(self._backend).__name__} does not support cancellation"
        )


# ── Vision adapter ─────────────────────────────────────────────────────────────


class VisionFromSenseBackend(Vision):
    """Wrap a :class:`~hermes_omni.types.SenseBackend` serving ``image_in``
    as a :class:`~hermes_omni.types.Vision` protocol.

    The wrapped backend must accept :class:`Part.image` and return text
    :class:`Part` objects (smolvlm, llama-server with multimodal).
    """

    def __init__(self, backend: SenseBackend) -> None:
        self._backend = backend

    async def describe(
        self, image_bytes: bytes, *, prompt: str | None = None
    ) -> str:
        """Wrap *image_bytes* in :class:`Part.image`, call the wrapped
        backend's ``process``, and return the description text."""
        meta: dict[str, Any] = {}
        if prompt is not None:
            meta["prompt"] = prompt
        part = Part.image(image_bytes, **meta)
        results = await _run_backend(self._backend, part)
        return _unwrap_text(results, what="VisionFromSenseBackend.describe")

    def describe_stream(
        self,
        frames: AsyncIterator[bytes],
        *,
        fps: float = 0.5,
    ) -> AsyncIterator[str]:
        """Describe each frame as it arrives, yielding one caption per
        frame.

        For live streaming video analysis the backend should implement
        the :class:`Vision` protocol directly; this simple adapter yields
        one caption per frame.
        """

        async def _stream() -> AsyncIterator[str]:
            async for frame in frames:
                yield await self.describe(frame)

        return _stream()

    async def cancel(self) -> None:
        """Delegate cancellation to the wrapped backend if it supports it."""
        cancel = getattr(self._backend, "cancel", None)
        if cancel is not None:
            result = cancel()
            if asyncio.iscoroutine(result):
                await result
            return
        raise UncancelableError(
            f"{type(self._backend).__name__} does not support cancellation"
        )


# ── Text adapter ───────────────────────────────────────────────────────────────


class TextFromSenseBackend(Text):
    """Wrap a :class:`~hermes_omni.types.SenseBackend` serving
    ``text_in``/``text_out`` as a :class:`~hermes_omni.types.Text` protocol.

    The wrapped backend must accept :class:`Part.text` and return text
    :class:`Part` objects (llama-server chat, agent).

    Parameters
    ----------
    backend
        The underlying :class:`SenseBackend` instance.
    brief
        Optional default system preface prepended to every ``chat`` call.
    """

    def __init__(
        self, backend: SenseBackend, *, brief: str | None = None
    ) -> None:
        self._backend = backend
        self._brief = brief

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        brief: str | None = None,
    ) -> AsyncIterator[str]:
        """Format the message history into text, call the wrapped backend's
        ``process``, and yield text chunks.

        Messages are formatted as ``Role: content`` lines with an optional
        system preface from *brief* (or ``self._brief``).
        """
        effective_brief = brief if brief is not None else self._brief
        lines: list[str] = []
        if effective_brief:
            lines.append(f"System: {effective_brief}")
        for msg in messages:
            role = str(msg.get("role", "user")).capitalize()
            content = msg.get("content", "")
            if isinstance(content, list):
                # multimodal content — flatten text parts
                text_parts = [
                    c["text"]
                    for c in content
                    if isinstance(c, dict) and "text" in c
                ]
                content = " ".join(text_parts)
            lines.append(f"{role}: {content}")
        text = "\n".join(lines)

        part = Part.text(text)
        results = await _run_backend(self._backend, part)
        for p in results:
            if p.is_text:
                yield p.text_of()

    async def cancel(self) -> None:
        """Delegate cancellation to the wrapped backend if it supports it."""
        cancel = getattr(self._backend, "cancel", None)
        if cancel is not None:
            result = cancel()
            if asyncio.iscoroutine(result):
                await result
            return
        raise UncancelableError(
            f"{type(self._backend).__name__} does not support cancellation"
        )