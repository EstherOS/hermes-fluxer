"""Component-graph session controller (omni config v2 only) and bridge utilities.

The :class:`Session` controller accepts a :class:`ComponentGraphProfile`,
resolves backends for each component via the registry, and exposes
``start()`` / ``stop()`` / ``feed_audio()`` / ``output_stream()`` /
``finalize_utterance()`` for the bridge to drive the voice pipeline.

No FSM — the bridge owns VAD/ASR/TTS lifecycle.

Bridge / backend utilities
--------------------------
PreBufferRing
    Ring buffer for the last 640 ms of PCM audio.
FallbackChain
    Ordered list of backend names tried in sequence on :class:`BackendError`.
CancellableMixin
    Base class for backends implementing the :class:`~hermes_omni.types.Cancellable` protocol.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from ..engine.graph import ComponentGraphProfile
from ..types import (
    BackendError,
    BackendNotConfigured,
    UncancelableError,
)

__all__ = [
    "CancellableMixin",
    "FallbackChain",
    "PreBufferRing",
    "Session",
]

logger = logging.getLogger(__name__)

#: 640 ms of raw PCM at 16 kHz, 16-bit mono = 640 × 16 = 10 240 bytes.
#: Ring buffer size for pre-buffering audio during interruptions.
PREBUFFER_SIZE: int = 10_240

#: Maximum seconds to wait for every active backend to acknowledge
#: cancellation before force-closing.
CANCEL_TIMEOUT: float = 2.0


class PreBufferRing:
    """Standalone ring buffer holding the last 640 ms of PCM audio.

    SIZE = 640 ms x 16000 Hz x 2 bytes/sample = 10240 bytes.
    Used by tests to verify ring behaviour independently of the FSM.
    """

    SIZE: int = PREBUFFER_SIZE

    def __init__(self) -> None:
        self._buf = bytearray(self.SIZE)
        self._pos = 0
        self._full = False

    def write(self, chunk: bytes) -> None:
        """Append PCM bytes, evicting oldest data to stay within SIZE."""
        if not chunk:
            return
        n = len(chunk)
        if n >= self.SIZE:
            self._buf[:] = chunk[-self.SIZE :]
            self._pos = 0
            self._full = True
            return
        available = self.SIZE - self._pos
        if n <= available:
            self._buf[self._pos : self._pos + n] = chunk
            self._pos += n
        else:
            first = chunk[:available]
            second = chunk[available:]
            self._buf[self._pos:] = first
            self._buf[: len(second)] = second
            self._pos = len(second)
        if self._pos >= self.SIZE:
            self._pos = 0
        if not self._full and self._pos + n >= self.SIZE:
            self._full = True

    def read_all(self) -> bytes:
        """Return entire ring contents in chronological order."""
        if not self._full:
            return bytes(self._buf[: self._pos])
        return bytes(self._buf[self._pos:] + self._buf[: self._pos])

    def clear(self) -> None:
        self._pos = 0
        self._full = False
        self._buf[:] = b"\x00" * self.SIZE

    @property
    def has_data(self) -> bool:
        return self._full or self._pos > 0


# ── Cancellable mixin ──────────────────────────────────────────────────────────


class CancellableMixin:
    """Base class for backends that implement the :class:`~hermes_omni.types.Cancellable` protocol.

    The default ``cancel()`` raises :class:`UncancelableError` — a backend
    that does **not** support cancellation (e.g. a blocking subprocess call)
    simply keeps this default, and the session controller ignores its output
    after the timeout rather than crashing.

    Subclasses that *do* support cancellation override ``cancel()`` and call
    ``_signal_cancel()`` after the backend has actually stopped::

        class MyTTSBackend(CancellableMixin):
            async def cancel(self) -> None:
                await self._subprocess.kill()
                self._signal_cancel()

    The mixin also exposes ``wait_cancelled`` (an :class:`asyncio.Event`)
    so the session controller can await all backends in the PREEMPTING state.

    After cancellation the backend can be reused in the same turn if the
    session controller calls :meth:`reset_cancel`.
    """

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    async def cancel(self) -> None:
        """Interrupt the current operation as quickly as possible.

        Raises :class:`UncancelableError` by default.  Override in subclasses
        that can actually honour interruption.
        """
        raise UncancelableError(
            f"{self.__class__.__name__} does not support cancellation; "
            "its output will be ignored after the cancel timeout"
        )

    def _signal_cancel(self) -> None:
        """Mark the backend as having completed cancellation.

        Sets the internal :class:`asyncio.Event` so that callers awaiting
        :attr:`wait_cancelled` can proceed.
        """
        self._cancelled.set()

    @property
    def wait_cancelled(self) -> asyncio.Event:
        """Event that is set when ``cancel()`` has completed.

        Usage::

            await mixin.wait_cancelled.wait()
        """
        return self._cancelled

    @property
    def is_cancelled(self) -> bool:
        """``True`` after :meth:`cancel` has completed."""
        return self._cancelled.is_set()

    def reset_cancel(self) -> None:
        """Reset the cancelled flag for reuse in a new turn.

        Called by the session controller after PREEMPTING → LISTENING when
        the same backend instance is used again.
        """
        self._cancelled.clear()


# ── Fallback chain ─────────────────────────────────────────────────────────────


#: Signature for a health-check callable: ``(backend_name) -> healthy``.
#: May be sync, async, or return an awaitable.
HealthCheckFn = Callable[[str], Any]

#: Signature for the execution callable: ``(backend_name) -> T``.
ExecFn = Callable[..., Any]


class FallbackChain:
    """Ordered list of backend names tried in sequence on failure.

    When the primary backend for a slot raises :class:`BackendError`, the
    chain logs the failure and tries the next candidate.  If every backend in
    the chain fails, :class:`BackendNotConfigured` is raised with the full
    chain details so the caller can degrade the slot to ``null``.

    An optional health-check callable can be provided — it is called before
    delegating to a backend; if it returns a falsy value the candidate is
    skipped without trying it.

    Parameters
    ----------
    candidates
        Ordered list of backend names — the first element is the primary,
        subsequent elements are fallbacks tried in order.
    health_check
        Optional ``(backend_name) -> bool | awaitable[bool]``.  When given,
        a candidate is skipped without trying if the check returns ``False``
        (or raises).  Accepts sync and async callables.

    Example
    -------
    >>> chain = FallbackChain(
    ...     ["local.qwen3asr", "local.whispercpp", "saas_google_stt"],
    ...     health_check=lambda name: name != "broken_backend",
    ... )
    >>> result = await chain.execute(lambda name: await asr(name))
    """

    def __init__(
        self,
        candidates: Sequence[str],
        *,
        health_check: HealthCheckFn | None = None,
    ) -> None:
        if not candidates:
            raise ValueError("FallbackChain needs at least one candidate")
        self.candidates = list(candidates)
        self.health_check = health_check

    def __repr__(self) -> str:
        return f"FallbackChain({self.candidates})"

    async def _check_health(self, name: str, slot: str, idx: int) -> bool:
        """Run the health check for *name*; return True if healthy."""
        try:
            result = self.health_check(name)  # type: ignore[misc]
            if callable(result):
                result = result()
            if isinstance(result, Awaitable):
                result = await result
            return bool(result)
        except Exception as exc:
            logger.warning(
                "healthcheck failed for %s (slot=%s, idx=%d): %s",
                name, slot, idx, exc,
            )
            return False

    async def execute(
        self,
        run: ExecFn,
        *,
        slot: str = "",
    ) -> Any:
        """Try each candidate in order until one succeeds.

        ``run`` is a callable ``(backend_name) -> T`` that returns the
        result or raises :class:`BackendError`.  On success the result is
        returned immediately; on failure the error is logged and the next
        candidate is tried.

        Raises
        ------
        BackendNotConfigured
            When every candidate has been exhausted.
        """
        errors: list[str] = []

        for idx, name in enumerate(self.candidates):
            # Health check (optional)
            if self.health_check is not None:
                if not await self._check_health(name, slot, idx):
                    logger.info(
                        "skipping unhealthy fallback %s (slot=%s, idx=%d)",
                        name, slot, idx,
                    )
                    errors.append(f"{name}: skipped (health check failed)")
                    continue

            # Execute the backend
            try:
                result = run(name)
                if isinstance(result, Awaitable):
                    result = await result
                logger.info(
                    "fallback chain succeeded on %s (slot=%s, idx=%d)",
                    name, slot, idx,
                )
                return result
            except BackendError as exc:
                msg = f"{name}: {exc}"
                logger.warning(
                    "fallback chain: candidate %s failed (slot=%s): %s",
                    name, slot, exc,
                )
                errors.append(msg)
                continue

        # All candidates exhausted
        raise BackendNotConfigured(
            f"all {len(self.candidates)} backend(s) failed for slot {slot!r}: "
            + "; ".join(errors)
        )


# ── Top-level Session controller (v2 / component graph only) ──────────────────


class Session:
    """Top-level session controller for a component-graph profile (v2).

    Accepts a :class:`ComponentGraphProfile`, resolves backends for each
    component in the graph, and exposes a streaming audio interface for
    the bridge to drive.  No FSM — the bridge owns VAD/ASR/TTS lifecycle.

    Parameters
    ----------
    profile
        A parsed component-graph profile (v2 only).
    get_backend
        Callable ``(name, **options) -> backend instance``.  When omitted
        defaults to :func:`hermes_omni.backends.registry.get_backend`.
    """

    def __init__(
        self,
        profile: ComponentGraphProfile,
        *,
        get_backend: Callable[..., Any] | None = None,
    ) -> None:
        self.profile = profile
        self._get_backend = get_backend
        self._running = False
        self._backends: dict[str, Any] = {}
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Component-graph backends (populated by _resolve_graph_backends)
        self._component_backends: dict[str, dict[str, Any]] = {}

        # Graph-mode: transcript callback (set by bridge via _hook_graph)
        self._graph_transcript_callback: Callable[[str], Any] | None = None

        # ASR task tracking
        self._asr_task: asyncio.Task | None = None
        self._asr_queue: asyncio.Queue | None = None
        self._pending_transcript: str | None = None
        self._asr_buffer: bytearray | None = None
        self._asr_collecting: bool = False

    # ── public API ──────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """``True`` after :meth:`start` and before :meth:`stop`."""
        return self._running

    async def start(self) -> None:
        """Resolve backends from the component graph and wire routes.

        Raises
        ------
        BackendNotConfigured
            When a required slot has no backend and no fallback succeeded.
        """
        if self._running:
            return
        self._running = True

        await self._resolve_graph_backends()
        self._wire_graph_routes()

        logger.info(
            "Session started  profile=%s  backends=%s",
            self.profile.name,
            sorted(self._backends),
        )

    async def stop(self) -> None:
        """Tear down all backends and halt the session.

        Cancels any active work, and clears the backend caches.
        """
        if not self._running:
            return
        self._running = False

        # Cancel every backend that supports it
        await self._cancel_all_backends()

        self._backends.clear()
        self._component_backends.clear()

        logger.info("Session stopped")

    async def feed_audio(self, chunk: bytes) -> None:
        """Feed a raw PCM chunk (@ 16 kHz, 16-bit mono) into the session.

        The chunk is forwarded to the active ASR queue if the bridge has
        started one (via VAD open → ``_asr_queue`` creation).  Chunks
        received before VAD opens are silently dropped.
        """
        if not self._running:
            return
        await self._forward_to_asr(chunk)

    async def start_asr(self) -> None:
        """Start the ASR streaming task on the ears component.

        Called by the bridge when VAD opens.  Creates the chunk queue and
        spins up a ``transcribe_stream`` task on the ears component's
        ``audio_in`` backend.
        """
        if self._asr_task is not None and not self._asr_task.done():
            return
        ears = self._component_backends.get("ears", {})
        audio_in = ears.get("audio_in")
        if audio_in is None:
            logger.debug("start_asr: no ears/audio_in backend")
            self._asr_queue = asyncio.Queue()
            self._asr_collecting = True
            self._asr_buffer = bytearray()
            self._pending_transcript = None
            return

        self._asr_queue = asyncio.Queue()
        self._asr_collecting = True
        self._asr_buffer = bytearray()
        self._pending_transcript = None

        if hasattr(audio_in, "transcribe_stream") and callable(audio_in.transcribe_stream):

            async def _chunk_stream() -> AsyncIterator[bytes]:
                while True:
                    chunk = await self._asr_queue.get()
                    if chunk is None:
                        break
                    yield chunk

            async def _asr_task_body() -> None:
                try:
                    async for partial in audio_in.transcribe_stream(  # type: ignore[arg-type]
                        _chunk_stream()
                    ):
                        if partial:
                            self._pending_transcript = partial
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.error("ASR stream failed: %s", exc)

            self._asr_task = asyncio.create_task(_asr_task_body())
            logger.info("start_asr: streaming ASR started on ears/audio_in")
        else:
            logger.debug(
                "start_asr: audio_in %s has no transcribe_stream — "
                "buffering until VAD close",
                type(audio_in).__name__,
            )

    def output_stream(self) -> AsyncIterator[bytes]:
        """Async iterator yielding audio output from the session.

        Yields raw PCM chunks as they are generated by the TTS backend.
        The iterator ends when the session stops or the output queue is
        drained.
        """
        async def _stream() -> AsyncIterator[bytes]:
            try:
                while self._running or not self._output_queue.empty():
                    try:
                        chunk = await asyncio.wait_for(
                            self._output_queue.get(),
                            timeout=1.0,
                        )
                        yield chunk
                    except asyncio.TimeoutError:
                        continue
            except GeneratorExit:
                pass

        return _stream()

    # ── backend resolution ──────────────────────────────────────────────

    async def _resolve_graph_backends(self) -> None:
        """Resolve backends for each component in the graph.

        For each component, examines its ``ins`` and ``outs`` to determine
        which backends it needs (audio_in, audio_out, text_in, text_out,
        image_in, video_in), then builds them.

        Results are stored in ``self._component_backends[component_name]``
        as a ``{slot_name: instance}`` dict.  Also populates
        ``self._backends`` so that ``_cancel_all_backends`` still works.
        """
        builder = self._get_backend
        if builder is None:
            from ..backends.registry import get_backend as _registry_get

            builder = _registry_get

        graph = self.profile.graph  # type: ignore[union-attr]
        self._component_backends = {}

        for cname in graph.component_names():
            comp = graph.get_component(cname)
            if comp is None:
                continue
            comp_backends: dict[str, Any] = {}

            # Determine which slots this component needs based on ins/outs
            needed_slots: set[str] = set()
            for sense in comp.ins:
                if sense == "audio":
                    needed_slots.add("audio_in")
                elif sense == "text":
                    needed_slots.add("text_in")
                elif sense == "image":
                    needed_slots.add("image_in")
                elif sense == "video":
                    needed_slots.add("video_in")
            for sense in comp.outs:
                if sense == "audio":
                    needed_slots.add("audio_out")
                elif sense == "text":
                    needed_slots.add("text_out")
                elif sense == "video":
                    needed_slots.add("video_out")

            for slot in needed_slots:
                backend_name = self._slot_to_backend_name(slot)
                try:
                    backend = await self._try_backend(builder, backend_name, {})
                    comp_backends[slot] = backend
                    self._backends[f"{cname}:{slot}"] = backend
                except BackendError as exc:
                    logger.warning(
                        "component %s: slot %s (%s) failed: %s",
                        cname, slot, backend_name, exc,
                    )

            self._component_backends[cname] = comp_backends

    @staticmethod
    def _slot_to_backend_name(slot: str) -> str:
        """Best-guess backend name for a slot when no explicit binding exists."""
        mapping: dict[str, str] = {
            "audio_in": "local.crispasr_stream",
            "audio_out": "local.kokoro",
            "text_in": "agent",
            "text_out": "agent",
            "image_in": "local.smolvlm",
            "video_in": "local.smolvlm_video",
            "video_out": "local.render",
        }
        return mapping.get(slot, "null")

    @staticmethod
    async def _try_backend(
        builder: Callable[..., Any],
        name: str,
        options: dict[str, Any],
    ) -> Any:
        """Build a single backend, raising :class:`BackendError` on failure."""
        try:
            result = builder(name, **options)
            if isinstance(result, Awaitable):
                result = await result
            return result
        except BackendNotConfigured:
            raise
        except Exception as exc:
            raise BackendError(
                f"backend {name!r} build failed: {exc}"
            ) from exc

    # ── Component-graph routing ───────────────────────────────────────

    def _wire_graph_routes(self) -> None:
        """Log the graph's endpoint assignments — slot backends live
        in ``_component_backends``, not individual fields."""
        graph = self.profile.graph  # type: ignore[union-attr]
        logger.info(
            "Graph routes wired  audio_in=%s  audio_out=%s  "
            "text_in=%s  core=%s",
            graph.audio_input_component() or "—",
            graph.audio_output_component() or "—",
            graph.text_input_component() or "—",
            graph.core_component() or "—",
        )

    # ── graph-mode helpers ────────────────────────────────────────────

    async def finalize_utterance(self) -> str | None:
        """Finalize the current utterance and return the transcript.

        Signals end-of-stream to the ASR backend, waits for the ASR task
        to complete, and returns the final transcript.  The transcript is
        also forwarded to ``_graph_transcript_callback`` if set (so the
        bridge can deliver it to the adapter).

        Returns ``None`` if no transcript was produced (silence).
        """
        # Signal end-of-stream to the chunk queue
        if self._asr_task is not None and not self._asr_task.done():
            if self._asr_queue is not None:
                await self._asr_queue.put(None)

            try:
                await self._asr_task
            except asyncio.CancelledError:
                logger.debug("ASR task was cancelled during finalize_utterance")
            except Exception as exc:
                logger.error("ASR task raised during finalize: %s", exc)

        transcript: str | None = None
        if self._pending_transcript is not None:
            transcript = self._pending_transcript

        # Buffered ASR fallback
        if transcript is None and self._asr_buffer is not None and self._asr_buffer:
            wav_bytes = bytes(self._asr_buffer)
            ears = self._component_backends.get("ears", {})
            audio_in = ears.get("audio_in")
            if audio_in is not None:
                if hasattr(audio_in, "transcribe") and callable(audio_in.transcribe):
                    try:
                        transcript = await audio_in.transcribe(
                            wav_bytes, sample_rate=16000
                        )
                    except Exception as exc:
                        logger.error("Graph ASR transcribe failed: %s", exc)
                elif hasattr(audio_in, "process") and callable(audio_in.process):
                    try:
                        from ..types import Part

                        parts = await audio_in.process(Part.audio(wav_bytes))
                        text_parts = [p.text_of() for p in parts if p.is_text]
                        transcript = " ".join(text_parts) if text_parts else ""
                    except Exception as exc:
                        logger.error("Graph ASR process failed: %s", exc)

        # Cleanup
        self._asr_task = None
        self._asr_collecting = False
        self._asr_buffer = None
        self._asr_queue = None

        if transcript is None:
            transcript = ""

        # Deliver via callback (bridge hook)
        if self._graph_transcript_callback is not None and transcript:
            await self._graph_transcript_callback(transcript)

        return transcript or None

    async def _push_to_component(
        self, component_name: str, sense: str, data: Any
    ) -> None:
        """Push data of a given sense to a component's input.

        Looks up the component's slot backend for *sense* and routes
        the data to it.  Supports audio, text, image, and video senses.
        """
        backends = self._component_backends.get(component_name, {})
        slot_map = {
            "audio": "audio_in",
            "text": "text_in",
            "image": "image_in",
            "video": "video_in",
        }
        slot = slot_map.get(sense)
        if slot is None:
            logger.warning(
                "No slot mapping for sense %r to component %s",
                sense, component_name,
            )
            return

        backend = backends.get(slot)
        if backend is None:
            logger.debug(
                "Component %s has no %s backend — can't push %s",
                component_name, slot, sense,
            )
            return

        if sense == "audio":
            await self._forward_to_asr(data if isinstance(data, bytes) else b"")
        elif sense == "text":
            text = str(data) if not isinstance(data, str) else data
            if hasattr(backend, "chat") and callable(backend.chat):
                logger.debug(
                    "Push text to component %s (chat backend): %.80s",
                    component_name, text,
                )
            else:
                logger.debug(
                    "Push text to component %s: %.80s",
                    component_name, text,
                )
        else:
            logger.debug(
                "Push sense=%s to component %s (%s bytes)",
                sense, component_name, len(data) if isinstance(data, bytes) else "?",
            )

    # ── internal helpers ────────────────────────────────────────────────

    async def _cancel_all_backends(self) -> None:
        """Call ``cancel()`` on every backend that has the method."""
        cancels: list[Awaitable[Any]] = []
        for name, backend in self._backends.items():
            cancel_method = getattr(backend, "cancel", None)
            if cancel_method is None:
                continue
            try:
                result = cancel_method()
                if isinstance(result, Awaitable):
                    cancels.append(result)
            except UncancelableError:
                logger.warning(
                    "backend %s is uncancellable — output will be ignored",
                    name,
                )
            except Exception as exc:
                logger.warning(
                    "backend %s cancel() raised %s: %s",
                    name, type(exc).__name__, exc,
                )

        if cancels:
            results = await asyncio.gather(*cancels, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(
                        "cancel() on backend %d raised: %s", i, result,
                    )

    async def _forward_to_asr(self, chunk: bytes) -> None:
        """Forward a PCM chunk to the active ASR stream or buffer.

        For streaming ASR: pushes into the chunk queue for the
        ``transcribe_stream`` task to consume.

        For buffered (non-streaming) backends: appends to the buffer
        for later batch transcription.
        """
        if self._asr_queue is not None:
            # Streaming path: push into the chunk queue
            await self._asr_queue.put(chunk)
        elif self._asr_collecting and self._asr_buffer is not None:
            # Buffered path: accumulate for later batch transcribe
            self._asr_buffer.extend(chunk)