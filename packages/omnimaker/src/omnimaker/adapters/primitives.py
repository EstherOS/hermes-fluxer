"""Engine-native routing primitive nodes — merge, tee, race, filter, etc."""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from omnimaker.adapters.base import NodeAdapter
from omnimaker.types import Envelope, ExecutionHandle


class MergeAdapter(NodeAdapter):
    """Combine envelopes from the two inputs (``a``, ``b``) into one output."""

    accepts = {"a": [], "b": []}
    emits = {"output": []}

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        yield envelope


class TeeAdapter(NodeAdapter):
    """Copy each input envelope to multiple named outputs."""

    accepts = {"input": []}
    emits = {"a": [], "b": [], "c": []}

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        # The engine handles fan-out via multiple routes; this node
        # just passes through. Each output port gets its own route.
        yield envelope


class FilterAdapter(NodeAdapter):
    """Pass through only envelopes matching a predicate.

    Config:
        predicate: str — Python expression evaluated against envelope
                    metadata (e.g. ``"type == 'text/final'"``).
    """

    accepts = {"input": []}
    emits = {"output": []}

    def __init__(self) -> None:
        super().__init__()
        self._predicate: str | None = None

    async def open(self, session_id: str, config: dict) -> None:
        self._predicate = config.get("predicate")

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        if self._predicate:
            try:
                # Simple type-match predicate
                if self._predicate.startswith("type =="):
                    expected = self._predicate.split("==", 1)[1].strip().strip("'\"")
                    if envelope.type == expected:
                        yield envelope
                else:
                    # Fallback: eval on envelope metadata
                    if eval(self._predicate, {"envelope": envelope}):
                        yield envelope
            except Exception:
                yield envelope
        else:
            yield envelope


class BufferAdapter(NodeAdapter):
    """Hold envelopes in a ring buffer and flush on trigger or fill.

    Config:
        max: int (default 16)
        flush_on: "fill" | "trigger" (default "fill")
    """

    accepts = {"input": []}
    emits = {"output": []}

    def __init__(self) -> None:
        super().__init__()
        self._buffer: list[Envelope] = []
        self._max = 16
        self._flush_on = "fill"

    async def open(self, session_id: str, config: dict) -> None:
        self._max = config.get("max", 16)
        self._flush_on = config.get("flush_on", "fill")

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        self._buffer.append(envelope)
        if self._flush_on == "fill" and len(self._buffer) >= self._max:
            for buf_env in self._buffer:
                yield buf_env
            self._buffer.clear()

    async def flush(self) -> list[Envelope]:
        flushed = self._buffer[:]
        self._buffer.clear()
        return flushed


class DebounceAdapter(NodeAdapter):
    """Drop envelopes arriving within a window of the previous pass-through.

    Config:
        window_ms: int (default 300)
    """

    accepts = {"input": []}
    emits = {"output": []}

    def __init__(self) -> None:
        super().__init__()
        self._window_s = 0.3
        self._last_yield: float = 0.0

    async def open(self, session_id: str, config: dict) -> None:
        self._window_s = max(0.0, float(config.get("window_ms", 300)) / 1000.0)

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        now = asyncio.get_event_loop().time()
        if now - self._last_yield >= self._window_s:
            self._last_yield = now
            yield envelope


class ThrottleAdapter(NodeAdapter):
    """Limit throughput to N envelopes per second.

    Config:
        rate: int (default 10)
    """

    accepts = {"input": []}
    emits = {"output": []}

    def __init__(self) -> None:
        super().__init__()
        self._rate = 10
        self._interval: float = 0.1
        self._last_yield: float = 0.0

    async def open(self, session_id: str, config: dict) -> None:
        self._rate = config.get("rate", 10)
        self._interval = 1.0 / self._rate

    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        now = asyncio.get_event_loop().time()
        if now - self._last_yield >= self._interval:
            self._last_yield = now
            yield envelope