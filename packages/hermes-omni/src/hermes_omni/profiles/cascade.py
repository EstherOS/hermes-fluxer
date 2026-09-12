"""The cascade facade: stitched profiles behind a duplex-like seam (spec §6).

:class:`Cascade` runs a half-duplex *turn* over a stitched profile:

1. **understand** — a non-text input part is transformed by its ``*_in`` slot
   (e.g. ``audio_in`` ASR → text); text inputs skip this step untouched;
2. **think** — optional host hook (``think=...``) representing the Hermes
   agent turn; the ``agent`` backend is a marker for exactly this step;
3. **express** — for every requested output sense, the ``*_out`` slot is run
   over the current text (e.g. ``audio_out`` TTS → a WAV part).

The input text part is never echoed back: text parts you receive are model or
host outputs.  ``output_senses=("audio",)`` gets you speech; ``"text"`` in the
list is a no-op (text is already streamed when it exists).

For callers that want the *unified* calling convention today,
:meth:`Cascade.session` returns a :class:`~hermes_omni.types.DuplexSession`:
a :class:`CascadeSession` for stitched profiles, or the unified backend's own
session (e.g. ``null_duplex``'s "not configured" error) for unified ones — so
downstream code does not know or care which mode it is talking to.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

from ..profiles import ResolvedProfile, resolve_profile
from ..types import (
    BackendError,
    BackendNotConfigured,
    DuplexSession,
    OmniError,
    Part,
)

__all__ = ["Cascade", "CascadeSession", "duplex_session"]

#: The optional host hook: ``(text, parts) -> str | Part | Sequence[Part] | awaitable``.
ThinkHook = Callable[[str, Sequence[Part]], Any]


async def duplex_session(
    profile: ResolvedProfile,
    *,
    think: ThinkHook | None = None,
    get_backend: Callable[..., Any] | None = None,
) -> DuplexSession:
    """Return a :class:`DuplexSession` for either profile mode.

    Stitched → :class:`CascadeSession`; unified → the backend's own session
    (opening it may raise :class:`BackendNotConfigured` for placeholders).
    """
    if profile.mode == "unified":
        if profile.backend is None:  # pragma: no cover - parse guarantees this
            raise BackendNotConfigured(f"profile {profile.name!r} is unified but names no backend")
        builder = get_backend or _default_get_backend()
        backend = builder(profile.backend.backend, **profile.backend.options)
        session = backend.open_session()
        if inspect.isawaitable(session):
            session = await session
        await session.open()
        return session
    session = CascadeSession(Cascade(profile, think=think, get_backend=get_backend))
    await session.open()
    return session


def _default_get_backend() -> Callable[..., Any]:
    from ..backends.registry import get_backend  # lazy to keep import order simple

    return get_backend


async def _coerce_parts(result: Any) -> list[Part]:
    """Normalize a backend/think result into a list of :class:`Part`."""
    if result is None:
        return []
    if inspect.isawaitable(result):
        return await _coerce_parts(await result)
    if isinstance(result, Part):
        return [result]
    if isinstance(result, str):
        return [Part.text(result)]
    if hasattr(result, "__aiter__"):
        return [p async for p in result]
    if isinstance(result, Iterable):
        parts: list[Part] = []
        for item in result:
            parts.extend(await _coerce_parts(item))
        return parts
    raise BackendError(f"cannot interpret backend result of type {type(result).__name__}")


class Cascade:
    """Turn-based duplex-like facade over a stitched profile."""

    def __init__(
        self,
        profile: ResolvedProfile,
        *,
        think: ThinkHook | None = None,
        get_backend: Callable[..., Any] | None = None,
        output_senses: Sequence[str] = (),
    ) -> None:
        if profile.mode != "stitched":
            raise OmniError(
                f"profile {profile.name!r} is {profile.mode}; Cascade turns are stitched-only — "
                "use Cascade.session()/duplex_session() for unified profiles"
            )
        self.profiles = profile
        self.think = think
        self.output_senses = tuple(dict.fromkeys(str(s).lower() for s in output_senses))
        self._get_backend = get_backend
        self._backends: dict[str, Any] = {}

    @classmethod
    def from_config(
        cls,
        cfg: Mapping[str, Any] | None,
        name: str | None = None,
        **kwargs: Any,
    ) -> "Cascade":
        return cls(resolve_profile(cfg, name), **kwargs)

    # ── backend plumbing ────────────────────────────────────────────────────

    async def _build(self, slot: str) -> Any:
        binding = self.profiles.require_binding(slot)
        if slot not in self._backends:
            builder = self._get_backend or _default_get_backend()

            def _build_one(name: str) -> Any:
                """Build a single backend by name.

                Preserves :class:`BackendNotConfigured` so the no-fallback
                path lets it propagate; the fallback path wraps it in
                :class:`BackendError` so that :class:`FallbackChain` can
                catch and retry.
                """
                try:
                    return builder(name, **binding.options)
                except BackendNotConfigured:
                    raise
                except Exception as exc:
                    raise BackendError(
                        f"cannot build {name!r} for slot {slot!r}: {exc}"
                    ) from exc

            if binding.fallback:
                from hermes_omni.session import FallbackChain  # lazy: avoid circular import

                def _fallback_one(name: str) -> Any:
                    """Like _build_one but converts BackendNotConfigured
                    to BackendError so FallbackChain retries."""
                    try:
                        return _build_one(name)
                    except BackendNotConfigured:
                        raise BackendError(
                            f"backend {name!r} is not configured"
                        ) from None

                chain = FallbackChain(
                    [binding.backend] + binding.fallback,
                )
                self._backends[slot] = await chain.execute(
                    _fallback_one, slot=slot
                )
            else:
                self._backends[slot] = _build_one(binding.backend)
        return self._backends[slot]

    @staticmethod
    async def _process(backend: Any, part: Part) -> list[Part]:
        result = backend.process(part)
        if inspect.isawaitable(result):
            result = await result
        return await _coerce_parts(result)

    # ── turns ───────────────────────────────────────────────────────────────

    async def push(
        self,
        part: Part,
        *,
        output_senses: Sequence[str] | None = None,
    ) -> AsyncIterator[Part]:
        """Run one half-duplex turn for ``part``, yielding output parts in order."""
        senses = self.output_senses if output_senses is None else tuple(str(s).lower() for s in output_senses)

        # 1. understand -------------------------------------------------------
        payload: list[Part] = []
        if part.is_text:
            payload = [part]
        else:
            slot = f"{part.kind}_in"
            backend = await self._build(slot)
            understood = await self._process(backend, part)
            for out in understood:
                yield out
            payload = [p for p in understood if p.is_text]

        # 2. think (host hook; the `agent` backend is a marker for this step) -
        if self.think is not None:
            text = "\n".join(p.text_of() for p in payload)
            thought = await _coerce_parts(self.think(text, list(payload)))
            for out in thought:
                yield out
            thought_text = [p for p in thought if p.is_text]
            if thought_text:
                payload = thought_text
            elif thought:
                payload = []  # think answered with non-text parts only

        # 3. express ----------------------------------------------------------
        for sense in senses:
            if sense == "text":
                continue  # text is already streamed when it exists
            slot = f"{sense}_out"
            backend = await self._build(slot)
            for text_part in payload:
                for out in await self._process(backend, text_part):
                    yield out

    async def collect(self, part: Part, *, output_senses: Sequence[str] | None = None) -> list[Part]:
        """Convenience: drain one turn into a list."""
        return [p async for p in self.push(part, output_senses=output_senses)]

    def session(self) -> "CascadeSession":
        """A :class:`DuplexSession` over this cascade (FIFO, half-duplex turns)."""
        return CascadeSession(self)


class CascadeSession:
    """Turn-based :class:`~hermes_omni.types.DuplexSession` over a :class:`Cascade`.

    ``send()`` runs a full turn and buffers its outputs; ``receive()`` drains
    them as an async iterator which ends when :meth:`close` is called.  Turns
    are serialized (half-duplex), which is the honest behavior of the stitched
    cascade on 6 GB (doc §2 "What duplex means here tonight").
    """

    def __init__(self, cascade: Cascade, *, queue_size: int = 0) -> None:
        self.cascade = cascade
        self.sent = 0
        self.emitted = 0
        self._queue: asyncio.Queue[Part | None] = asyncio.Queue(maxsize=queue_size)
        self._closed = False
        self._send_lock = asyncio.Lock()

    async def open(self) -> None:
        self._closed = False

    async def send(self, part: Part) -> None:
        if self._closed:
            raise OmniError("session is closed")
        async with self._send_lock:
            async for out in self.cascade.push(part):
                await self._queue.put(out)
            self.sent += 1

    def receive(self) -> AsyncIterator[Part]:
        async def _drain() -> AsyncIterator[Part]:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                self.emitted += 1
                yield item

        return _drain()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._queue.put(None)
