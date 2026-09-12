"""Async bridge between a talker frontend and a thinker backend.

The :class:`ThinkerBridge` connects a talker session to a thinker profile,
allowing the talker to delegate work that needs 'real work' (tools, reasoning,
heavy context) to a separate thinker with its own (potentially different)
backends.

Flow
----
1. The talker pauses its own conversational response.
2. It bundles the relevant media (audio clip, image, video frame) alongside
   the text query and sends it to the thinker via :meth:`delegate` or
   :meth:`think_in_background`.
3. The bridge processes media parts (raw passthrough if the thinker profile
   declares matching backends; otherwise the talker's backends transcribe /
   describe the media into text).
4. The combined query + media text is sent to the thinker's ``text_out``
   (chat) backend.
5. The bridge yields the response text tokens back to the talker.

Raw-media passthrough logic
---------------------------
* If the thinker profile has an ``audio_in`` backend, the raw audio
  :class:`~hermes_omni.types.Part` is passed directly to that backend
  without talker-side transcription.
* If the thinker profile has an ``image_in`` backend, the raw image
  :class:`~hermes_omni.types.Part` is passed directly to that backend
  without talker-side captioning.
* Otherwise, the talker's own ``audio_in`` / ``_eyes`` (vision) backends
  transcribe or describe the media into text.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from ..types import (
    BackendError,
    BackendNotConfigured,
    OmniError,
    Part,
)
from ..profiles import ResolvedProfile

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────


async def _run_backend(backend: Any, part: Part) -> Sequence[Part]:
    """Call ``backend.process(part)`` and normalise the result into a list.

    Handles sync / async / async-iterator returns, mirroring the same
    pattern in :func:`hermes_omni.backends.adapters._run_backend`.
    """
    result = backend.process(part)
    if isinstance(result, asyncio.Future) or asyncio.iscoroutine(result):
        result = await result
    if hasattr(result, "__aiter__"):
        return [p async for p in result]
    if hasattr(result, "__iter__"):
        return list(result)
    if isinstance(result, Part):
        return [result]
    if result is None:
        return []
    raise BackendError(
        f"unexpected result type from {type(backend).__name__}.process: "
        f"{type(result).__name__}"
    )


def _unwrap_text(results: Sequence[Part]) -> str | None:
    """Return the first text part's content, or *None*."""
    for p in results:
        if p.is_text:
            return p.text_of()
    return None


def _data_bytes(data: Any) -> bytes | None:
    """Normalise a Part's data to ``bytes`` (reads files if needed)."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, memoryview):
        return bytes(data)
    if isinstance(data, Path):
        return data.read_bytes()
    if isinstance(data, str):
        return Path(data).read_bytes()
    return None


# ── result holder for background tasks ───────────────────────────────────────


class BridgeResult:
    """Thread-safe holder for the outcome of a background thinking task.

    The talker can poll the result synchronously via :attr:`text` / :attr:`error`
    or await asynchronously via :meth:`wait`.
    """

    def __init__(self) -> None:
        self.text: str | None = None
        self.error: Exception | None = None
        self._event = asyncio.Event()

    def set_result(self, text: str) -> None:
        """Store the successful result and wake any waiter."""
        self.text = text
        self._event.set()

    def set_error(self, error: Exception) -> None:
        """Store the failure and wake any waiter."""
        self.error = error
        self._event.set()

    async def wait(self, timeout: float | None = None) -> str | None:
        """Wait (up to *timeout* seconds) for the result.

        Returns the text on success, *None* on timeout, and raises the
        stored exception on failure.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if self.error is not None:
            raise self.error
        return self.text


# ── the bridge ───────────────────────────────────────────────────────────────


class ThinkerBridge:
    """Async bridge between a talker frontend and a thinker backend.

    Parameters
    ----------
    talker_session
        The active session instance that owns the talker frontend.  The bridge
        accesses ``talker_session._audio_in`` and ``talker_session._eyes`` for
        talker-side media transcription when the thinker profile lacks matching
        media backends.
    thinker_profile
        A resolved profile describing the thinker's backends.  At minimum it
        should bind ``text_out`` (the chat/thinker backend).  Optional bindings
        ``audio_in`` and ``image_in`` enable raw-media passthrough.
    get_backend
        A callable ``(name, **options) -> backend instance``, typically
        :func:`hermes_omni.backends.registry.get_backend`.
    """

    def __init__(
        self,
        talker_session: Any,
        thinker_profile: ResolvedProfile,
        get_backend: Callable[..., Any],
    ) -> None:
        self._talker_session = talker_session
        self._thinker_profile = thinker_profile
        self._get_backend = get_backend

        # ── resolved backends (populated lazily by _resolve) ───────────
        self._thinker_backend: Any = None  # text_out / main chat backend
        self._media_backends: dict[str, Any] = {}  # "audio_in" | "image_in" → backend
        self._resolved: bool = False

        # ── background thinking ─────────────────────────────────────────
        self._background_result: BridgeResult | None = None
        self._background_task: asyncio.Task | None = None

    # ── resolution ─────────────────────────────────────────────────────────

    async def _resolve(self) -> None:
        """Build the thinker's backends from its profile (lazy, idempotent)."""
        if self._resolved:
            return
        self._resolved = True

        profile = self._thinker_profile

        if profile.mode == "unified":
            await self._resolve_unified(profile)
        else:
            await self._resolve_stitched(profile)

    async def _resolve_unified(self, profile: ResolvedProfile) -> None:
        """Resolve a unified-mode thinker (single duplex backend)."""
        if profile.backend is None:
            raise BackendNotConfigured(
                f"thinker profile {profile.name!r} is unified but has no backend"
            )
        backend = self._get_backend(
            profile.backend.backend, **profile.backend.options
        )
        if isinstance(backend, Awaitable):
            backend = await backend
        self._thinker_backend = backend

    async def _resolve_stitched(self, profile: ResolvedProfile) -> None:
        """Resolve a stitched-mode thinker (individual slot backends)."""
        for slot, binding in profile.bindings.items():
            try:
                backend = self._get_backend(
                    binding.backend, **binding.options
                )
                if isinstance(backend, Awaitable):
                    backend = await backend
            except Exception as exc:
                logger.warning(
                    "thinker slot %s backend %s failed: %s",
                    slot, binding.backend, exc,
                )
                continue

            if slot == "text_out":
                self._thinker_backend = backend
            elif slot in ("audio_in", "image_in"):
                self._media_backends[slot] = backend

    # ── media processing (raw passthrough / talker fallback) ────────────────

    async def _process_media(self, media: list[Part]) -> str:
        """Convert media parts to a combined text annotation.

        For each part the bridge first checks whether the thinker profile has
        a matching raw-media backend.  If so, the raw part is passed directly
        to that backend.  Otherwise the talker's own backends are used to
        transcribe (audio) or describe (image/video) the content.

        Returns a single string with the combined text, separated by newlines.
        """
        texts: list[str] = []

        for part in media:
            if part.is_text:
                texts.append(part.text_of())
                continue

            if part.kind == "audio":
                text = await self._process_audio(part)
                if text:
                    texts.append(f"[Audio transcript: {text}]")
            elif part.kind in ("image", "video"):
                text = await self._process_image(part)
                if text:
                    texts.append(f"[Image description: {text}]")

        return "\n".join(texts)

    async def _process_audio(self, part: Part) -> str | None:
        """Transcribe audio — raw passthrough to thinker's audio_in, or
        fall back to the talker's ``_audio_in`` backend."""
        # ── thinker raw passthrough ─────────────────────────────────────
        if "audio_in" in self._media_backends:
            backend = self._media_backends["audio_in"]
            try:
                # New protocol: AudioIn.transcribe()
                if hasattr(backend, "transcribe") and callable(backend.transcribe):
                    wav_data = _data_bytes(part.data)
                    if wav_data is not None:
                        text = await backend.transcribe(wav_data)
                        if text:
                            return text
                # Old protocol: SenseBackend.process()
                elif hasattr(backend, "process") and callable(backend.process):
                    results = await _run_backend(backend, part)
                    text = _unwrap_text(results)
                    if text:
                        return text
            except Exception as exc:
                logger.debug("thinker audio_in passthrough failed: %s", exc)

        # ── talker fallback ─────────────────────────────────────────────
        audio_in = getattr(self._talker_session, "_audio_in", None)
        if audio_in is None:
            return None

        # New protocol: AudioIn.transcribe()
        if hasattr(audio_in, "transcribe") and callable(audio_in.transcribe):
            wav_data = _data_bytes(part.data)
            if wav_data is not None:
                try:
                    transcript = await audio_in.transcribe(
                        wav_data, sample_rate=16000
                    )
                    return transcript if transcript else None
                except Exception as exc:
                    logger.debug("talker audio_in.transcribe failed: %s", exc)

        # Old protocol: SenseBackend.process()
        if hasattr(audio_in, "process") and callable(audio_in.process):
            try:
                results = await _run_backend(audio_in, part)
                return _unwrap_text(results)
            except Exception as exc:
                logger.debug("talker audio_in.process failed: %s", exc)

        return None

    async def _process_image(self, part: Part) -> str | None:
        """Describe image/video — raw passthrough to thinker's image_in, or
        fall back to the talker's ``_eyes`` (vision) backend."""
        # ── thinker raw passthrough ─────────────────────────────────────
        if "image_in" in self._media_backends:
            backend = self._media_backends["image_in"]
            try:
                # New protocol: Vision.describe()
                if hasattr(backend, "describe") and callable(backend.describe):
                    img_data = _data_bytes(part.data)
                    if img_data is not None:
                        text = await backend.describe(img_data)
                        if text:
                            return text
                # Old protocol: SenseBackend.process()
                elif hasattr(backend, "process") and callable(backend.process):
                    results = await _run_backend(backend, part)
                    text = _unwrap_text(results)
                    if text:
                        return text
            except Exception as exc:
                logger.debug("thinker image_in passthrough failed: %s", exc)

        # ── talker fallback ─────────────────────────────────────────────
        eyes = getattr(self._talker_session, "_eyes", None)
        if eyes is None:
            return None

        # New protocol: Vision.describe()
        if hasattr(eyes, "describe") and callable(eyes.describe):
            img_data = _data_bytes(part.data)
            if img_data is not None:
                try:
                    description = await eyes.describe(img_data)
                    return description if description else None
                except Exception as exc:
                    logger.debug("talker eyes.describe failed: %s", exc)

        # Old protocol: SenseBackend.process()
        if hasattr(eyes, "process") and callable(eyes.process):
            try:
                results = await _run_backend(eyes, part)
                return _unwrap_text(results)
            except Exception as exc:
                logger.debug("talker eyes.process failed: %s", exc)

        return None

    # ── delegate ───────────────────────────────────────────────────────────

    async def delegate(
        self, query: str, media: list[Part]
    ) -> AsyncIterator[str]:
        """Send *query* + *media* to the thinker and yield response tokens.

        Parameters
        ----------
        query
            The text query from the talker.
        media
            Zero or more :class:`Part` objects carrying audio, image, or
            text content.

        Yields
        ------
        str
            Text tokens as they are generated by the thinker's chat backend.
        """
        await self._resolve()

        # Combine query text with media transcriptions/descriptions
        media_text = await self._process_media(media)
        if media_text:
            full_query = f"{query}\n\n{media_text}"
        else:
            full_query = query

        thinker = self._thinker_backend
        if thinker is None:
            raise BackendNotConfigured(
                "thinker has no text_out backend resolved — "
                "the thinker profile must bind at least 'text_out'"
            )

        # ── Text.chat() protocol (async generator) ──────────────────────
        if hasattr(thinker, "chat") and callable(thinker.chat):
            messages = [{"role": "user", "content": full_query}]
            brief: str | None = (
                self._thinker_profile.raw.get("brief")
                if hasattr(self._thinker_profile, "raw")
                else None
            )
            async for token in thinker.chat(messages=messages, brief=brief):
                if token:
                    yield token
            return

        # ── SenseBackend.process() fallback ─────────────────────────────
        if hasattr(thinker, "process") and callable(thinker.process):
            text_part = Part.text(full_query)
            results = await _run_backend(thinker, text_part)
            for p in results:
                if p.is_text:
                    yield p.text_of()
            return

        raise BackendNotConfigured(
            f"thinker backend {type(thinker).__name__} has neither "
            f"chat() nor process() — cannot produce a response"
        )

    # ── background thinking ────────────────────────────────────────────────

    async def think_in_background(
        self, query: str, media: list[Part]
    ) -> asyncio.Task:
        """Submit *query* + *media* to the thinker in a background task.

        The talker can say 'let me think about that' and continue the
        conversation.  When the thinker finishes, the result is available
        via :meth:`check_background`, :attr:`background_result`, or the
        returned :class:`asyncio.Task`.

        Parameters
        ----------
        query
            The text query.
        media
            Zero or more media parts.

        Returns
        -------
        asyncio.Task
            The background task.  The task's result is ``None`` on success
            (the text is stored in :attr:`background_result`) or an
            ``Exception`` on failure.
        """
        self._background_result = BridgeResult()

        async def _task_body() -> None:
            try:
                collected: list[str] = []
                async for token in self.delegate(query, media):
                    collected.append(token)
                text = "".join(collected)
                self._background_result.set_result(text)
            except Exception as exc:
                self._background_result.set_error(exc)

        task = asyncio.create_task(_task_body())
        self._background_task = task
        return task

    async def check_background(
        self, timeout: float | None = None
    ) -> str | None:
        """Return the background result if ready, optionally waiting.

        Parameters
        ----------
        timeout
            Seconds to wait for completion.  ``None`` (default) returns
            immediately if not done; a numeric value waits up to that many
            seconds.

        Returns
        -------
        str or None
            The complete response text, or *None* if not yet available.
        """
        if self._background_result is None:
            return None
        return await self._background_result.wait(timeout=timeout)

    @property
    def background_result(self) -> str | None:
        """The complete text from the last background task, or ``None``."""
        if self._background_result is not None:
            return self._background_result.text
        return None

    @property
    def background_task(self) -> asyncio.Task | None:
        """The active background task, or ``None``."""
        return self._background_task

    @property
    def thinker_backend(self) -> Any:
        """The resolved thinker chat backend (after :meth:`_resolve`)."""
        return self._thinker_backend

    @property
    def is_background_done(self) -> bool:
        """``True`` when the background task has completed (success or error)."""
        if self._background_task is None:
            return False
        return self._background_task.done()