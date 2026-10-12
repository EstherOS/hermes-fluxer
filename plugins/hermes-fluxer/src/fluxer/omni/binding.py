"""Fluxer platform binding adapter for the hermes-omni engine."""

from __future__ import annotations

from typing import Any, Callable, AsyncGenerator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.types import Envelope, ExecutionHandle


class FluxerBinding(NodeAdapter):
    """Platform adapter exposing Fluxer voice/video endpoints.

    Registered as the ``@fluxer.*`` binding. Routes ``@fluxer.audio_out``
    envelopes to the attached output handler (the voice bridge), which
    pushes them to the LiveKit speaker track.

    The binding owns platform audio I/O only. It never orchestrates the
    turn pipeline — it is a stream endpoint for the graph.
    """

    accepts: dict[str, list[str]] = {}
    emits: dict[str, list[str]] = {}

    session_scope = "persistent"

    def __init__(self) -> None:
        super().__init__()
        self.session_id: str | None = None
        self._output_handlers: dict[str, Callable[[Envelope], None]] = {}

    async def open(self, session_id: str, config: dict) -> None:
        self.session_id = session_id

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        # Route audio out to the attached handler (voice bridge)
        if envelope.type == "audio" and self._output_handlers:
            for handler in list(self._output_handlers.values()):
                try:
                    handler(envelope)
                except Exception:
                    pass
        if False:
            yield

    async def close(self) -> None:
        self._output_handlers.clear()

    # ── Output handler wiring ──────────────────────────────────────────

    def attach_output_handler(self, session_id: str,
                              handler: Callable[[Envelope], None]) -> None:
        """Attach a sink for ``@fluxer.audio_out`` envelopes (the bridge)."""
        self._output_handlers[session_id] = handler

    def detach_output_handler(self, session_id: str) -> None:
        self._output_handlers.pop(session_id, None)