"""Session FSM, cancellation protocol, fallback chains, and top-level session controller.

Implements the session-level finite state machine (spec §4), cancellation
protocol (spec §5), fallback chains (spec §6), and the top-level
:class:`Session` controller that wires a resolved profile to the FSM.

Components
----------
SessionState
    Enum of FSM states: IDLE, LISTENING, ANALYZING, THINKING, SPEAKING,
    PREEMPTING, HALTED.
SessionFSM
    Async state machine with pre-buffer ring and transition callbacks
    matching the spec §4 transition table.
CancellableMixin
    Base class for backends that implement the :class:`~hermes_omni.types.Cancellable`
    protocol.  Provides a default ``cancel()`` that raises
    :class:`~hermes_omni.types.UncancelableError`.
FallbackChain
    Ordered list of backend names tried in sequence on :class:`BackendError`,
    with optional per-candidate health checks.
Session
    Top-level controller that accepts a :class:`~hermes_omni.profiles.ResolvedProfile`,
    creates an FSM, wires the profile's backends to FSM transitions, and
    exposes ``start()`` / ``stop()`` / ``feed_audio()`` / ``output_stream()``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from enum import Enum, auto
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Sequence,
)

from ..types import (
    AudioIn,
    AudioOut,
    BackendError,
    BackendNotConfigured,
    OmniError,
    Text,
    UncancelableError,
)
from ..profiles import ResolvedProfile
from .thinker_bridge import ThinkerBridge, BridgeResult

__all__ = [
    "BridgeResult",
    "CancellableMixin",
    "FallbackChain",
    "PreBufferRing",
    "Session",
    "SessionFSM",
    "SessionState",
    "ThinkerBridge",
]

logger = logging.getLogger(__name__)

#: 640 ms of raw PCM at 16 kHz, 16-bit mono = 640 × 16 = 10 240 bytes.
#: Every session FSM maintains a ring buffer of this size so an interruption
#: mid-speech does not eat the beginning of the user's next utterance.
PREBUFFER_SIZE: int = 10_240

#: Maximum seconds to wait for every active backend to acknowledge
#: cancellation before force-closing (spec §5.2).
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
            self._buf[:] = chunk[-self.SIZE:]
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


# ── State enumeration ──────────────────────────────────────────────────────────


class SessionState(Enum):
    """Finite-state-machine states for a realtime/omni session (spec §4).

    States
    ------
    IDLE
        No activity — waiting for VAD open.
    LISTENING
        Audio_in active — collecting utterance into the ASR stream.
    ANALYZING
        VAD closed — audio_in producing the transcript.
    THINKING
        Talker/thinker active — generating a response.
    SPEAKING
        Audio_out active — playing synthesised speech.
    PREEMPTING
        Interruption detected — cancelling all in-flight backends.
    HALTED
        Unrecoverable error — session is dead.
    """

    IDLE = auto()
    LISTENING = auto()
    ANALYZING = auto()
    THINKING = auto()
    SPEAKING = auto()
    PREEMPTING = auto()
    HALTED = auto()


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
    """Ordered list of backend names tried in sequence on failure (spec §6).

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


# ── Session FSM ────────────────────────────────────────────────────────────────


class SessionFSM:
    """Async state machine for a realtime/omni session (spec §4).

    The FSM coordinates three pipelines — audio in, talker/thinker, audio out —
    and maintains a **pre-buffer ring** of the last 640 ms of raw PCM audio
    (10 240 bytes @ 16 kHz).  This ensures that an interruption triggered
    mid-speech does not eat the beginning of the user's next utterance.

    Transition rules (spec §4 table)
    ---------------------------------
    ==========  =========================  ===========  =============================
    From        Event                     To           Action
    ==========  =========================  ===========  =============================
    IDLE        VAD open (speech start)   LISTENING    Start audio_in + pre-buffer
    LISTENING   VAD close (silence)       ANALYZING    Request transcript from ASR
    ANALYZING   transcript ready          THINKING     Inject text into agent
    THINKING    first token ready         SPEAKING     Start audio_out in parallel
    THINKING    full answer ready         SPEAKING     Start audio_out (no prior)
    SPEAKING    VAD open (interrupt)      PREEMPTING   Cancel all active backends
    SPEAKING    audio_out stream ends     IDLE         Log complete turn
    PREEMPTING  all cancels return        LISTENING    Flush pre-buffer → new audio_in
    PREEMPTING  cancel timeout (2s)       LISTENING    Force-close, flush pre-buffer
    *           fatal backend error       HALTED       Report, no auto-recovery
    ==========  =========================  ===========  =============================

    The FSM does **not** own the backends — it fires transition callbacks
    that the :class:`Session` controller wires up.  This keeps the FSM itself
    testable without any real backends.

    Parameters
    ----------
    prebuffer_size
        Size of the PCM ring buffer in bytes (default 10 240).
    cancel_timeout
        Seconds to wait for backends to acknowledge cancellation (default 2.0).
    """

    def __init__(
        self,
        *,
        prebuffer_size: int = PREBUFFER_SIZE,
        cancel_timeout: float = CANCEL_TIMEOUT,
    ) -> None:
        # ── state ───────────────────────────────────────────────────────
        self._state = SessionState.IDLE

        # ── pre-buffer ring ─────────────────────────────────────────────
        self._prebuffer_size = prebuffer_size
        self._prebuffer: deque[bytes] = deque()
        self._prebuffer_bytes = 0

        # ── cancellation timeout ────────────────────────────────────────
        self._cancel_timeout = cancel_timeout
        self._cancel_complete = asyncio.Event()
        self._cancel_timeout_expired = asyncio.Event()
        self._halted_event = asyncio.Event()
        self._cancelling = False

        # ── transition callbacks (wired by Session) ─────────────────────
        self.on_vad_open: Callable[[], Any] | None = None
        self.on_vad_close: Callable[[], Any] | None = None
        self.on_transcript_ready: Callable[[str], Any] | None = None
        self.on_first_token: Callable[[], Any] | None = None
        self.on_full_answer: Callable[[], Any] | None = None
        self.on_speech_end: Callable[[], Any] | None = None
        self.on_cancel_all: Callable[[], Any] | None = None
        self.on_preemption_done: Callable[[], Any] | None = None
        self.on_fatal_error: Callable[[Exception], Any] | None = None

        logger.debug("SessionFSM created (prebuffer=%d, cancel_to=%.1f)", prebuffer_size, cancel_timeout)

    # ── state ──────────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        """Current FSM state."""
        return self._state

    @property
    def halted(self) -> bool:
        """``True`` when the session is in HALTED state."""
        return self._state is SessionState.HALTED

    # ── pre-buffer ring ────────────────────────────────────────────────

    @property
    def prebuffer_bytes(self) -> int:
        """Number of bytes currently held in the pre-buffer ring."""
        return self._prebuffer_bytes

    def feed_prebuffer(self, chunk: bytes) -> None:
        """Push a PCM chunk into the ring buffer, evicting oldest data.

        The buffer never exceeds ``prebuffer_size`` bytes.
        """
        size = len(chunk)
        if size <= 0:
            return
        self._prebuffer.append(chunk)
        self._prebuffer_bytes += size
        while self._prebuffer_bytes > self._prebuffer_size:
            oldest = self._prebuffer.popleft()
            self._prebuffer_bytes -= len(oldest)

    def drain_prebuffer(self) -> AsyncIterator[bytes]:
        """Yield every byte in the pre-buffer as an async stream, then clear.

        Used on PREEMPTING → LISTENING to feed trailing audio into the new
        ASR stream.
        """
        async def _drain() -> AsyncIterator[bytes]:
            while self._prebuffer:
                yield self._prebuffer.popleft()
            self._prebuffer_bytes = 0

        return _drain()

    # ── helpers ────────────────────────────────────────────────────────

    def _check_alive(self) -> None:
        if self._state is SessionState.HALTED:
            raise OmniError("session is halted — no transitions possible")

    @staticmethod
    async def _fire(cb: Callable | None, *args: Any) -> None:
        """Invoke an optional transition callback (sync or async)."""
        if cb is not None:
            result = cb(*args)
            if isinstance(result, Awaitable):
                await result

    def _set_state(self, new: SessionState) -> None:
        old = self._state
        self._state = new
        logger.debug("FSM %s → %s", old.name, new.name)

    # ── transition events (called by Session or transport layer) ───────

    async def trigger_vad_open(self) -> None:
        """VAD detected start of speech — IDLE → LISTENING."""
        self._check_alive()
        if self._state is SessionState.IDLE:
            self._set_state(SessionState.LISTENING)
            await self._fire(self.on_vad_open)

    async def trigger_vad_close(self) -> None:
        """VAD detected end of utterance — LISTENING → ANALYZING."""
        self._check_alive()
        if self._state is SessionState.LISTENING:
            self._set_state(SessionState.ANALYZING)
            await self._fire(self.on_vad_close)

    async def trigger_transcript(self, text: str) -> None:
        """ASR transcript ready — ANALYZING → THINKING.

        *text* is the transcribed utterance injected into the agent context.
        """
        self._check_alive()
        if self._state is SessionState.ANALYZING:
            self._set_state(SessionState.THINKING)
            await self._fire(self.on_transcript_ready, text)

    async def trigger_first_token(self) -> None:
        """First token from the thinker/talker — THINKING → SPEAKING.

        Starts audio_out in parallel with the rest of the generation.
        """
        self._check_alive()
        if self._state is SessionState.THINKING:
            self._set_state(SessionState.SPEAKING)
            await self._fire(self.on_first_token)

    async def trigger_full_answer(self) -> None:
        """Full response ready (no SPEAKING yet) — THINKING → SPEAKING.

        Used when the generation finishes before any streaming TTS was
        started (e.g. for cached or very short replies).
        """
        self._check_alive()
        if self._state is SessionState.THINKING:
            self._set_state(SessionState.SPEAKING)
            await self._fire(self.on_full_answer)

    async def trigger_interruption(self) -> None:
        """VAD open during SPEAKING — SPEAKING → PREEMPTING → LISTENING.

        Calls ``on_cancel_all``, waits for backends to acknowledge (up to
        ``cancel_timeout``), then transitions to LISTENING and fires
        ``on_preemption_done``.
        """
        self._check_alive()
        if self._state is SessionState.SPEAKING:
            self._set_state(SessionState.PREEMPTING)
            await self._fire(self.on_cancel_all)
            await self._await_cancellation()

    async def trigger_speech_end(self) -> None:
        """Audio_out stream finished naturally — SPEAKING → IDLE."""
        self._check_alive()
        if self._state is SessionState.SPEAKING:
            self._set_state(SessionState.IDLE)
            await self._fire(self.on_speech_end)

    async def trigger_fatal_error(self, error: Exception) -> None:
        """Any unrecoverable error — * → HALTED.

        The session is dead after this call.  There is no auto-recovery.
        """
        was_already = self._state is SessionState.HALTED
        self._set_state(SessionState.HALTED)
        self._halted_event.set()
        if not was_already:
            await self._fire(self.on_fatal_error, error)

    # ── cancellation internals ─────────────────────────────────────────

    async def _await_cancellation(self) -> None:
        """Wait for all backend cancel() calls with a timeout.

        On timeout, the session force-closes and proceeds to LISTENING
        anyway (spec §5.2).
        """
        self._cancelling = True
        self._cancel_complete = asyncio.Event()
        self._cancel_timeout_expired = asyncio.Event()

        try:
            await asyncio.wait_for(
                self._cancel_complete.wait(),
                timeout=self._cancel_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "cancel timeout (%.1f s) — force-closing backends",
                self._cancel_timeout,
            )
            self._cancel_timeout_expired.set()

        self._cancelling = False
        self._set_state(SessionState.LISTENING)
        await self._fire(self.on_preemption_done)

    def signal_cancellation_complete(self) -> None:
        """Signal that all active backends have acknowledged cancellation.

        Called by the :class:`Session` controller after it has called
        ``cancel()`` on every active backend.
        """
        self._cancel_complete.set()

    async def wait_until_halted(self) -> None:
        """Block the current coroutine until the session enters HALTED."""
        await self._halted_event.wait()


# ── Top-level Session controller ───────────────────────────────────────────────


class Session:
    """Top-level session controller (spec §10, item 3).

    Accepts a :class:`ResolvedProfile`, creates an :class:`SessionFSM`, wires
    the profile's backends to the FSM transitions, and exposes a streaming
    audio interface.

    Dispatch logic (spec §3.1, item 3)
    -----------------------------------
    * If the profile has ``mode: unified`` and a ``realtime`` or ``omni``
      backend is declared, the FSM is wired for duplex — the unified backend
      handles audio in/out directly and stitched roles (ears, mouth, talker)
      are registered for text/info relay only.
    * If the profile has ``mode: stitched``, each slot is resolved
      independently and the cascade handles the turn flow.

    Parameters
    ----------
    profile
        Resolved profile to execute.  Determines mode (``stitched`` /
        ``unified``), which backends to build, and fallback chains.
    get_backend
        Callable ``(name, **options) -> backend instance``.  When omitted
        defaults to :func:`hermes_omni.backends.registry.get_backend`.
    prebuffer_size
        Pre-buffer ring size in bytes (default 10 240 = 640 ms @ 16 kHz).
    cancel_timeout
        Seconds to wait for backend cancellation (default 2.0).
    """

    def __init__(
        self,
        profile: ResolvedProfile,
        *,
        get_backend: Callable[..., Any] | None = None,
        prebuffer_size: int = PREBUFFER_SIZE,
        cancel_timeout: float = CANCEL_TIMEOUT,
        fallback_chains: dict[str, FallbackChain] | None = None,
    ) -> None:
        self.profile = profile
        self._get_backend = get_backend
        self._fallback_chains = dict(fallback_chains or {})
        self._fsm = SessionFSM(
            prebuffer_size=prebuffer_size,
            cancel_timeout=cancel_timeout,
        )
        self._running = False
        self._backends: dict[str, Any] = {}
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Resolved slot backends (set during _resolve_backends)
        self._audio_in: Any = None
        self._audio_out: Any = None
        self._talker: Any = None
        self._thinker: Any = None
        self._realtime_backend: Any = None
        self._eyes: Any = None
        self._text: Any = None

        # ── ASR / thinker / TTS task tracking ───────────────────────────
        self._asr_task: asyncio.Task | None = None
        self._thinker_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._tts_stream: asyncio.Queue | None = None  # shared channel: thinker → TTS
        self._pending_transcript: str | None = None
        self._asr_queue: asyncio.Queue | None = None  # feed_audio → ASR streaming
        self._asr_collecting: bool = False
        self._asr_buffer: bytearray | None = None

    # ── public API ──────────────────────────────────────────────────────

    @property
    def fsm(self) -> SessionFSM:
        """Read-only access to the underlying state machine."""
        return self._fsm

    @property
    def running(self) -> bool:
        """``True`` after :meth:`start` and before :meth:`stop`."""
        return self._running

    async def start(self) -> None:
        """Resolve backends from the profile, wire FSM transitions, enter IDLE.

        Raises
        ------
        BackendNotConfigured
            When a required slot has no backend and no fallback succeeded.
        """
        if self._running:
            return
        self._running = True

        await self._resolve_backends()
        self._wire_fsm()

        logger.info(
            "Session started  profile=%s  mode=%s  state=%s  backends=%s",
            self.profile.name,
            self.profile.mode,
            self._fsm.state.name,
            sorted(self._backends),
        )

    async def stop(self) -> None:
        """Tear down all backends and halt the session.

        Cancels any active work, signals HALTED on the FSM, and clears
        the backend cache.
        """
        if not self._running:
            return
        self._running = False

        # Cancel every backend that supports it
        await self._cancel_all_backends()

        # Halt the FSM if still alive
        if not self._fsm.halted:
            await self._fsm.trigger_fatal_error(
                OmniError("session stopped by user")
            )

        self._backends.clear()
        self._audio_in = self._audio_out = None
        self._talker = self._thinker = None
        self._realtime_backend = self._eyes = self._text = None

        logger.info("Session stopped")

    async def feed_audio(self, chunk: bytes) -> None:
        """Feed a raw PCM chunk (@ 16 kHz, 16-bit mono) into the session.

        The chunk is added to the pre-buffer ring unconditionally.  If the
        FSM is in LISTENING state and an ``audio_in`` backend is active, the
        chunk is also forwarded to the ASR stream.

        Chunks received during PREEMPTING are buffered in the ring and will
        be flushed into the new ASR stream when PREEMPTING → LISTENING fires.
        """
        if not self._running or self._fsm.halted:
            return

        self._fsm.feed_prebuffer(chunk)

        if self._fsm.state is SessionState.LISTENING:
            if self._audio_in is not None:
                await self._forward_to_asr(chunk)
            elif (
                self._realtime_backend is not None
                and hasattr(self._realtime_backend, "feed_audio")
            ):
                await self._realtime_backend.feed_audio(chunk)

    def output_stream(self) -> AsyncIterator[bytes]:
        """Async iterator yielding audio output from the session.

        Yields raw PCM chunks (typically @ 24 kHz or the codec rate of the
        active ``audio_out`` backend) as they are generated.  The iterator
        ends when the session stops or the output queue is drained.

        This is the seam a transport layer (e.g. LiveKit, Discord voice)
        reads from to get speaker audio.
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

    async def _resolve_backends(self) -> None:
        """Build backend instances from ``self.profile``.

        Uses either the injected ``_get_backend`` or the registry's
        :func:`~hermes_omni.backends.registry.get_backend` as the builder.

        For each slot in the profile:
        1. Try the primary backend.
        2. On :class:`BackendError`, consult ``_fallback_chains[slot]``
           (if configured) and try each candidate in order.
        3. If all fail, log a warning and leave the slot at ``None`` — the
           FSM will handle the gap gracefully.
        """
        builder = self._get_backend
        if builder is None:
            from ..backends.registry import get_backend as _registry_get

            builder = _registry_get

        profile = self.profile

        if profile.mode == "unified":
            await self._resolve_unified(profile, builder)
        else:
            await self._resolve_stitched(profile, builder)

    async def _resolve_unified(
        self,
        profile: ResolvedProfile,
        builder: Callable[..., Any],
    ) -> None:
        """Resolve a unified-mode profile (realtime/omni backend)."""
        if profile.backend is not None:
            try:
                backend = builder(profile.backend.backend, **profile.backend.options)
                self._backends["realtime"] = backend
                self._realtime_backend = backend
            except BackendError as exc:
                logger.warning(
                    "unified backend %s failed: %s",
                    profile.backend.backend,
                    exc,
                )

    async def _resolve_stitched(
        self,
        profile: ResolvedProfile,
        builder: Callable[..., Any],
    ) -> None:
        """Resolve a stitched-mode profile (individual slot backends)."""
        for slot, binding in profile.bindings.items():
            chain = self._fallback_chains.get(slot)
            try:
                backend = await self._try_backend(builder, binding.backend, binding.options)
            except BackendError as primary_err:
                if chain is not None:
                    backend = await self._try_fallback_chain(
                        chain, builder, slot, primary_err,
                    )
                else:
                    logger.warning(
                        "slot %s: primary backend %s failed and no fallback chain: %s",
                        slot, binding.backend, primary_err,
                    )
                    continue

            self._backends[slot] = backend
            self._assign_slot(slot, backend)

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

    async def _try_fallback_chain(
        self,
        chain: FallbackChain,
        builder: Callable[..., Any],
        slot: str,
        primary_err: Exception,
    ) -> Any:
        """Run a fallback chain for *slot*, starting from the primary error."""
        return await chain.execute(
            lambda name: self._try_backend(builder, name, {}),
            slot=slot,
        )

    def _assign_slot(self, slot: str, backend: Any) -> None:
        """Route a built backend to the matching slot attribute."""
        mapping: dict[str, str] = {
            "audio_in": "_audio_in",
            "audio_out": "_audio_out",
            "text_in": "_talker",
            "text_out": "_thinker",
            "image_in": "_eyes",
            "video_in": "_eyes",
            "realtime": "_realtime_backend",
        }
        attr = mapping.get(slot)
        if attr is not None:
            setattr(self, attr, backend)

    # ── FSM wiring ──────────────────────────────────────────────────────

    def _wire_fsm(self) -> None:
        """Connect FSM transition callbacks to backend actions.

        Only wires transitions whose slot backends are actually resolved.
        Unresolved slots simply skip their transitions (graceful degradation).
        """
        fsm = self._fsm

        has_realtime = self._realtime_backend is not None
        has_audio_in = self._audio_in is not None
        has_audio_out = self._audio_out is not None
        has_thinker = self._thinker is not None

        if has_realtime:
            # Realtime backend handles audio — minimal FSM (duplex mode)
            fsm.on_vad_open = self._on_vad_open_realtime
            fsm.on_cancel_all = self._on_cancel_all
            fsm.on_preemption_done = self._on_preemption_done
        else:
            # Stitched FSM (ears/mouth/talker/thinker cascade)
            if has_audio_in:
                fsm.on_vad_open = self._on_vad_open
                fsm.on_vad_close = self._on_vad_close

            fsm.on_transcript_ready = self._on_transcript_ready

            if has_audio_out:
                fsm.on_first_token = self._on_first_token
                fsm.on_full_answer = self._on_full_answer

            fsm.on_cancel_all = self._on_cancel_all

            fsm.on_speech_end = self._on_speech_end
            fsm.on_preemption_done = self._on_preemption_done
            fsm.on_fatal_error = self._on_fatal_error

        logger.debug(
            "FSM wired (realtime=%s, audio_in=%s, audio_out=%s, thinker=%s)",
            has_realtime, has_audio_in, has_audio_out, has_thinker,
        )

    # ── FSM action handlers ─────────────────────────────────────────────

    async def _on_vad_open(self) -> None:
        """VAD opened — start ASR collection.

        If ``self._audio_in`` exposes a streaming interface (``transcribe_stream``),
        start an :class:`asyncio.Task` that reads PCM chunks from an internal
        :class:`asyncio.Queue` and yields incremental transcripts into
        ``self._pending_transcript``.

        Otherwise (file-based / :class:`SenseBackend`), set ``_asr_collecting``
        and buffer incoming chunks until VAD close.
        """
        logger.debug("VAD open — starting listen cycle")

        # Fresh queue for incoming PCM chunks
        self._asr_queue = asyncio.Queue()
        self._pending_transcript = None
        self._asr_collecting = False
        self._asr_buffer = None

        audio_in = self._audio_in
        if audio_in is None:
            logger.debug("No audio_in backend — will emit empty transcript")
            return

        # ── Streaming ASR (new protocol: AudioIn.transcribe_stream) ─────
        if hasattr(audio_in, "transcribe_stream") and callable(
            audio_in.transcribe_stream  # type: ignore[arg-type]
        ):

            async def _chunk_stream() -> AsyncIterator[bytes]:
                """Yield PCM chunks from the ASR queue until sentinel."""
                while True:
                    chunk = await self._asr_queue.get()
                    if chunk is None:  # sentinel → end of stream
                        break
                    yield chunk

            async def _asr_task_body() -> None:
                """Run the streaming transcription task."""
                try:
                    async for partial in audio_in.transcribe_stream(
                        _chunk_stream()  # type: ignore[arg-type]
                    ):
                        if partial:
                            self._pending_transcript = partial
                except Exception as exc:
                    logger.error("ASR stream failed: %s", exc)
                    await self._fsm.trigger_fatal_error(
                        BackendError(f"ASR streaming failed: {exc}")
                    )

            self._asr_task = asyncio.create_task(_asr_task_body())
            return

        # ── SenseBackend or blocking transcribe — buffer chunks ────────
        logger.debug(
            "audio_in %s has no transcribe_stream — buffering until VAD close",
            type(audio_in).__name__,
        )
        self._asr_collecting = True
        self._asr_buffer = bytearray()

    async def _on_vad_open_realtime(self) -> None:
        logger.debug("VAD open (realtime mode) — backend handles audio flow")

    async def _on_vad_close(self) -> None:
        """VAD closed — finalize ASR collection and obtain the transcript.

        For streaming ASR: signal end-of-stream into the chunk queue, wait
        for the ASR task to finish, and use the last partial transcript as
        the final one.

        For buffered ASR: assemble buffered PCM bytes and call ``transcribe``
        (or ``SenseBackend.process``).

        Calls ``await self._fsm.trigger_transcript(transcript)`` with the
        result (or ``""`` if no speech was detected).
        """
        logger.debug("VAD close — requesting transcript from ASR")

        audio_in = self._audio_in
        if audio_in is None:
            logger.debug("No audio_in — emitting empty transcript")
            await self._fsm.trigger_transcript("")
            return

        transcript: str | None = None

        # ── Streaming ASR ───────────────────────────────────────────────
        if self._asr_task is not None and not self._asr_task.done():
            # Signal end-of-stream to the chunk stream
            if self._asr_queue is not None:
                await self._asr_queue.put(None)

            try:
                await self._asr_task
            except asyncio.CancelledError:
                logger.debug("ASR task was cancelled during VAD close")
            except Exception as exc:
                logger.error("ASR task raised during finalize: %s", exc)

        if self._pending_transcript is not None:
            transcript = self._pending_transcript
            self._pending_transcript = None

        # ── Buffered ASR (blocking transcribe) ──────────────────────────
        if transcript is None and self._asr_collecting and self._asr_buffer is not None:
            wav_bytes = bytes(self._asr_buffer)

            # New protocol: AudioIn.transcribe()
            if hasattr(audio_in, "transcribe") and callable(audio_in.transcribe):
                try:
                    transcript = await audio_in.transcribe(
                        wav_bytes, sample_rate=16000  # type: ignore[arg-type]
                    )
                except Exception as exc:
                    logger.error("ASR transcribe failed: %s", exc)
                    transcript = ""

            # Old protocol: SenseBackend.process()
            elif hasattr(audio_in, "process") and callable(audio_in.process):
                try:
                    from ..types import Part

                    parts = await audio_in.process(Part.audio(wav_bytes))  # type: ignore[arg-type]
                    text_parts = [p.text_of() for p in parts if p.is_text]
                    transcript = " ".join(text_parts) if text_parts else ""
                except Exception as exc:
                    logger.error("ASR SenseBackend process failed: %s", exc)
                    transcript = ""

        # ── Cleanup ──────────────────────────────────────────────────────
        self._asr_task = None
        self._asr_collecting = False
        self._asr_buffer = None
        self._asr_queue = None

        if transcript is None:
            transcript = ""

        await self._fsm.trigger_transcript(transcript)

    async def _on_transcript_ready(self, text: str) -> None:
        """Transcript is ready — inject into the thinker and start generation.

        If ``self._thinker`` has a ``chat()`` method (new :class:`Text`
        protocol), start an :class:`asyncio.Task` that:

        1. Builds a message list with the transcript as the user message.
        2. Calls ``thinker.chat(messages, brief=...)`` and iterates over
           yielded tokens.
        3. On the first non-empty token calls
           ``await self._fsm.trigger_first_token()``.
        4. Feeds every token into ``self._tts_stream`` (shared queue the TTS
           task reads from).

        If no thinker is available, the transcript is emitted directly as a
        text :class:`Part` into ``self._output_queue`` (fallback text-only
        mode).
        """
        logger.debug("Transcript ready (%d chars)", len(text))

        if not text.strip():
            logger.debug("Empty transcript — skipping thinker, returning to IDLE")
            return

        thinker = self._thinker
        if thinker is None:
            # Fallback: no thinker — emit the transcript as text output
            logger.debug("No thinker backend — emitting transcript as text")
            part_bytes = text.encode("utf-8")
            await self._output_queue.put(part_bytes)
            return

        # ── Text protocol: chat() async generator ───────────────────────
        if hasattr(thinker, "chat") and callable(thinker.chat):
            # Shared channel: thinker puts tokens → TTS task reads them
            self._tts_stream = asyncio.Queue()

            brief: str | None = None
            if hasattr(self.profile, "raw"):
                brief = self.profile.raw.get("brief") or None  # type: ignore[attr-defined]

            async def _thinker_task_body() -> None:
                """Run thinker.chat, feed tokens into TTS queue."""
                try:
                    messages = [{"role": "user", "content": text}]
                    triggered = False

                    async for token in thinker.chat(  # type: ignore[arg-type]
                        messages=messages,
                        brief=brief,
                    ):
                        if not token:
                            continue

                        # Push token into the TTS stream queue
                        await self._tts_stream.put(token)

                        # First non-empty token triggers THINKING → SPEAKING
                        if not triggered:
                            triggered = True
                            await self._fsm.trigger_first_token()

                except asyncio.CancelledError:
                    logger.debug("Thinker task cancelled")
                except Exception as exc:
                    logger.error("Thinker task failed: %s", exc)
                    await self._fsm.trigger_fatal_error(
                        BackendError(f"thinker failure: {exc}")
                    )
                finally:
                    # Signal end of text stream to the TTS consumer
                    if self._tts_stream is not None:
                        await self._tts_stream.put(None)

            self._thinker_task = asyncio.create_task(_thinker_task_body())
            return

        # ── SenseBackend protocol: process() ────────────────────────────
        if hasattr(thinker, "process") and callable(thinker.process):
            self._tts_stream = asyncio.Queue()

            async def _thinker_process_body() -> None:
                """Run thinker.process, feed result into TTS queue."""
                try:
                    from ..types import Part

                    parts = await thinker.process(  # type: ignore[arg-type]
                        Part.text(text)
                    )
                    triggered = False
                    for p in parts:
                        if p.is_text:
                            token = p.text_of()
                            if token:
                                await self._tts_stream.put(token)
                                if not triggered:
                                    triggered = True
                                    await self._fsm.trigger_first_token()
                except asyncio.CancelledError:
                    logger.debug("Thinker (process) task cancelled")
                except Exception as exc:
                    logger.error("Thinker process failed: %s", exc)
                    await self._fsm.trigger_fatal_error(
                        BackendError(f"thinker failure: {exc}")
                    )
                finally:
                    if self._tts_stream is not None:
                        await self._tts_stream.put(None)

            self._thinker_task = asyncio.create_task(_thinker_process_body())
            return

        logger.warning(
            "Thinker %s has neither chat() nor process() — emitting raw text",
            type(thinker).__name__,
        )
        part_bytes = text.encode("utf-8")
        await self._output_queue.put(part_bytes)

    async def _on_first_token(self) -> None:
        """First token from thinker — start streaming audio or text output.

        If ``self._audio_out`` has ``synthesize_stream`` (new :class:`AudioOut`
        protocol), start an :class:`asyncio.Task` that:

        1. Creates a text-stream async generator from ``self._tts_stream``.
        2. Calls ``audio_out.synthesize_stream(text_stream)``.
        3. Puts each yielded audio chunk into ``self._output_queue``.

        If no ``audio_out`` is available, stream text tokens directly into
        ``self._output_queue`` as encoded bytes.
        """
        logger.debug("First token — starting streaming audio_out")

        if self._tts_stream is None:
            logger.warning("_on_first_token called but _tts_stream is None — no-op")
            return

        audio_out = self._audio_out

        # ── Streaming TTS (AudioOut.synthesize_stream) ──────────────────
        if audio_out is not None and hasattr(audio_out, "synthesize_stream") and callable(
            audio_out.synthesize_stream  # type: ignore[arg-type]
        ):

            async def _tts_task_body() -> None:
                """Pull text tokens, synthesize, push audio chunks."""
                try:

                    async def _text_stream() -> AsyncIterator[str]:
                        """Read text tokens from the shared TTS queue."""
                        while True:
                            token = await self._tts_stream.get()
                            if token is None:  # sentinel
                                break
                            yield token

                    async for audio_chunk in audio_out.synthesize_stream(  # type: ignore[arg-type]
                        _text_stream()
                    ):
                        if audio_chunk:
                            await self._output_queue.put(audio_chunk)

                except asyncio.CancelledError:
                    logger.debug("TTS streaming task cancelled")
                except Exception as exc:
                    logger.error("TTS streaming failed: %s", exc)
                    await self._fsm.trigger_fatal_error(
                        BackendError(f"TTS streaming failure: {exc}")
                    )

            self._tts_task = asyncio.create_task(_tts_task_body())
            return

        # ── Blocking TTS — full_answer will handle this ────────────────
        # If only synthesize() is available (no streaming variant), the
        # first-token callback is a no-op; _on_full_answer will collect the
        # full text and call blocking synthesize later.

        # ── No audio_out — stream text tokens to output queue ──────────
        if audio_out is None:
            async def _text_output_task_body() -> None:
                """Stream text tokens from TTS queue to output queue."""
                try:
                    while True:
                        token = await self._tts_stream.get()
                        if token is None:
                            break
                        chunk = token.encode("utf-8")
                        await self._output_queue.put(chunk)
                except asyncio.CancelledError:
                    logger.debug("Text-output task cancelled")

            self._tts_task = asyncio.create_task(_text_output_task_body())

    async def _on_full_answer(self) -> None:
        """Full answer ready — start blocking TTS (no streaming).

        Collects all remaining text tokens from ``self._tts_stream`` and
        passes the complete text to ``audio_out.synthesize()``.  The returned
        WAV bytes are chunked (~100 ms pieces) and put into
        ``self._output_queue``.

        If no ``audio_out`` is available, the full text is emitted as encoded
        bytes directly (text-only fallback).
        """
        logger.debug("Full answer — starting blocking audio_out")

        if self._tts_stream is None:
            logger.warning("_on_full_answer called but _tts_stream is None — no-op")
            return

        # ── Drain all tokens from the TTS queue ─────────────────────────
        full_text_parts: list[str] = []
        while True:
            try:
                token = await asyncio.wait_for(
                    self._tts_stream.get(), timeout=0.5
                )
                if token is None:
                    break
                full_text_parts.append(token)
            except asyncio.TimeoutError:
                break

        full_text = "".join(full_text_parts)
        if not full_text:
            logger.debug("Full answer called with empty text — no-op")
            return

        audio_out = self._audio_out

        # ── Blocking TTS (AudioOut.synthesize) ──────────────────────────
        if audio_out is not None and hasattr(audio_out, "synthesize") and callable(
            audio_out.synthesize  # type: ignore[arg-type]
        ):

            async def _blocking_tts_body() -> None:
                """Synthesize full text and chunk into output queue."""
                try:
                    wav_bytes = await audio_out.synthesize(  # type: ignore[arg-type]
                        full_text
                    )
                    # 3200 bytes = ~100 ms @ 16 kHz 16-bit mono
                    chunk_size = 3200
                    for i in range(0, len(wav_bytes), chunk_size):
                        await self._output_queue.put(
                            wav_bytes[i : i + chunk_size]
                        )
                except asyncio.CancelledError:
                    logger.debug("Blocking TTS task cancelled")
                except Exception as exc:
                    logger.error("Blocking TTS failed: %s", exc)
                    await self._fsm.trigger_fatal_error(
                        BackendError(f"TTS synthesis failure: {exc}")
                    )

            asyncio.ensure_future(_blocking_tts_body())
            return

        # ── No audio_out — emit full text as raw bytes ─────────────────
        logger.debug("No audio_out — emitting full answer as text")
        await self._output_queue.put(full_text.encode("utf-8"))

    async def _on_cancel_all(self) -> None:
        """Cancel every active backend and running task.

        1. Cancel :class:`asyncio.Task` instances for ASR, thinker, and TTS
           streaming (if running).
        2. Call ``cancel()`` on every active backend via
           :meth:`_cancel_all_backends`.
        3. Reset shared state fields.
        4. Signal cancellation complete on the FSM.
        """
        logger.debug("Cancelling all active backends and tasks")

        # ── Cancel in-flight asyncio tasks ──────────────────────────────
        for task in (self._asr_task, self._thinker_task, self._tts_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # ── Cancel backends ─────────────────────────────────────────────
        await self._cancel_all_backends()

        # ── Reset shared state ──────────────────────────────────────────
        self._asr_task = None
        self._thinker_task = None
        self._tts_task = None
        self._tts_stream = None
        self._pending_transcript = None
        self._asr_queue = None
        self._asr_collecting = False
        self._asr_buffer = None

        self._fsm.signal_cancellation_complete()

    async def _on_preemption_done(self) -> None:
        """Preemption complete — flush pre-buffer into new ASR stream.

        1. Asynchronously drain the FSM's pre-buffer ring (trailing audio
           that arrived during the preemption window).
        2. Forward each chunk into the new ASR stream via ``_forward_to_asr``.
        3. Start a new ASR streaming task (equivalent to ``_on_vad_open``)
           so the session resumes listening.
        """
        logger.debug("Pre-buffer flush starting (%d bytes)", self._fsm.prebuffer_bytes)

        # Create a fresh ASR queue for the new listen cycle
        self._asr_queue = asyncio.Queue()
        self._pending_transcript = None
        self._asr_collecting = False
        self._asr_buffer = None

        audio_in = self._audio_in
        has_streaming = (
            audio_in is not None
            and hasattr(audio_in, "transcribe_stream")
            and callable(audio_in.transcribe_stream)  # type: ignore[arg-type]
        )

        if has_streaming:

            async def _post_preemption_asr() -> None:
                """Drain pre-buffer, then stream live chunks through ASR."""
                try:
                    # Step 1: build a combined chunk stream that drains the
                    #         pre-buffer first, then reads live chunks
                    async def _combined_chunk_stream() -> AsyncIterator[bytes]:
                        # Drain pre-buffer into the stream
                        async for chunk in self._fsm.drain_prebuffer():
                            yield chunk
                        # Then stream incoming live chunks
                        while True:
                            chunk = await self._asr_queue.get()
                            if chunk is None:
                                break
                            yield chunk

                    # Step 2: pipe through transcribe_stream
                    async for partial in audio_in.transcribe_stream(  # type: ignore[arg-type]
                        _combined_chunk_stream()
                    ):
                        if partial:
                            self._pending_transcript = partial

                except asyncio.CancelledError:
                    logger.debug("Post-preemption ASR task cancelled")
                except Exception as exc:
                    logger.error(
                        "Post-preemption ASR stream failed: %s", exc
                    )
                    await self._fsm.trigger_fatal_error(
                        BackendError(
                            f"Post-preemption ASR failed: {exc}"
                        )
                    )

            self._asr_task = asyncio.create_task(_post_preemption_asr())
        else:
            # Non-streaming backend: drain pre-buffer, start buffering
            logger.debug(
                "audio_in %s has no transcribe_stream — buffering pre-drain",
                type(audio_in).__name__ if audio_in else "None",
            )
            self._asr_collecting = True
            self._asr_buffer = bytearray()
            async for chunk in self._fsm.drain_prebuffer():
                if self._asr_buffer is not None:
                    self._asr_buffer.extend(chunk)

    async def _on_speech_end(self) -> None:
        """Speech output ended — turn complete.

        Logs the event and cleans up any completed tasks.  No further action
        is needed — the FSM returns to IDLE and waits for the next VAD open.
        """
        logger.debug("Speech output ended — turn complete")

    async def _on_fatal_error(self, error: Exception) -> None:
        """Fatal session error — log and stop.

        Logs the error at ERROR level, then tears down the session via
        :meth:`stop`.  The FSM is already in HALTED state when this handler
        runs.
        """
        logger.error("Fatal session error: %s", error)
        await self.stop()

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
        """Forward a PCM chunk to the active ASR stream.

        The chunk is already in the pre-buffer ring — this method sends it
        into the ASR streaming queue so that the streaming transcription task
        can process it.

        For buffered (non-streaming) backends, the chunk is appended to
        ``self._asr_buffer`` for later batch transcription.
        """
        if self._asr_queue is not None:
            # Streaming path: push into the chunk queue
            await self._asr_queue.put(chunk)
        elif self._asr_collecting and self._asr_buffer is not None:
            # Buffered path: accumulate for later batch transcribe
            self._asr_buffer.extend(chunk)