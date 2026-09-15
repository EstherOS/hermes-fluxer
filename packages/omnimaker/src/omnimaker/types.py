"""Core data types for the hermes-omni streaming message graph engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


# ── Envelope ───────────────────────────────────────────────────────────

@dataclass
class Envelope:
    """A typed message unit moving through the graph."""

    type: str
    payload: Any
    session_id: str
    turn_id: str
    execution_id: str
    source: str | None = None          # endpoint that produced this envelope
    parent_execution_id: str | None = None
    timestamp_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_type(self, new_type: str) -> "Envelope":
        return Envelope(
            type=new_type,
            payload=self.payload,
            session_id=self.session_id,
            turn_id=self.turn_id,
            execution_id=self.execution_id,
            source=self.source,
            parent_execution_id=self.parent_execution_id,
            timestamp_ms=self.timestamp_ms,
            metadata=self.metadata,
        )


# ── Endpoint ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Endpoint:
    """A named I/O port on a node or binding."""

    node: str   # node name, or "@binding" for external
    port: str

    @property
    def is_external(self) -> bool:
        return self.node.startswith("@")

    def __str__(self) -> str:
        return f"{self.node}.{self.port}"

    @classmethod
    def parse(cls, raw: str) -> "Endpoint":
        """Parse 'node.port' or '@binding.port'."""
        if "." not in raw:
            raise ValueError(f"Invalid endpoint '{raw}' — expected 'node.port'")
        node, port = raw.rsplit(".", 1)
        return cls(node=node, port=port)

    @classmethod
    def from_stream(cls, stream_name: str) -> "Endpoint":
        return cls(node=stream_name, port="stream")


# ── Transport / concurrency ────────────────────────────────────────────

InterruptAction = Literal["replace", "queue", "drop", "parallel"]
ConcurrencyMode = Literal["queue", "replace", "drop", "parallel"]
AwaitMode = Literal["sync", "async"]
ResponseAs = Literal["tool_result", "input", "interrupt_signal"]
SessionScope = Literal["ephemeral", "persistent"]


@dataclass
class ConcurrencyConfig:
    interrupt_on: list[str] = field(default_factory=list)
    on_interrupt: InterruptAction = "queue"


@dataclass
class TransportConfig:
    concurrency: dict | None = None


# ── Route ──────────────────────────────────────────────────────────────

RouteKind = Literal["dataflow", "invocation"]


@dataclass
class Route:
    """A directed edge in the graph."""

    source: Endpoint
    dest: Endpoint
    kind: RouteKind = "dataflow"
    when: dict | None = None           # structured selector: {type: ...}
    transport: TransportConfig = field(default_factory=TransportConfig)

    # Invocation-only fields
    await_mode: AwaitMode | None = None
    response_to: Endpoint | None = None
    response_as: ResponseAs | None = None

    def matches(self, envelope: Envelope) -> bool:
        """Check whether an envelope should be routed through this edge."""
        if self.when and "type" in self.when:
            return envelope.type == self.when["type"]
        return True


# ── Node configuration ─────────────────────────────────────────────────

@dataclass
class ToolDef:
    name: str
    route_to: str
    description: str = ""
    await_mode: AwaitMode = "async"
    response_to: str | None = None    # "node.port"
    response_as: ResponseAs = "tool_result"


@dataclass
class NodeConfig:
    name: str
    use: str
    config: dict = field(default_factory=dict)
    session: SessionScope = "ephemeral"
    consumes: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    tools: list[ToolDef] = field(default_factory=list)


# ── Stream configuration ───────────────────────────────────────────────

@dataclass
class StreamConfig:
    name: str
    type: str                 # MIME-style: "audio/stream", "video/stream", etc.
    source: str               # "@binding.port"
    optional: bool = False


# ── Profile ────────────────────────────────────────────────────────────

@dataclass
class ProfileConfig:
    name: str
    streams: dict[str, StreamConfig] = field(default_factory=dict)
    nodes: dict[str, NodeConfig] = field(default_factory=dict)
    routes: list[Route] = field(default_factory=list)


# ── Execution handle ───────────────────────────────────────────────────

@dataclass
class ExecutionHandle:
    """Tracks an active node invocation for cancellation and correlation."""

    id: str
    session_id: str
    node_name: str
    parent_id: str | None = None
    _cancelled: bool = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @classmethod
    def create(cls, session_id: str, node_name: str,
               parent_id: str | None = None) -> "ExecutionHandle":
        return cls(
            id=str(uuid.uuid4()),
            session_id=session_id,
            node_name=node_name,
            parent_id=parent_id,
        )


# ── Errors ─────────────────────────────────────────────────────────────

class OmniError(Exception):
    """Base error for the omni engine."""

class ProfileError(OmniError):
    """Invalid profile configuration."""

class RouteError(OmniError):
    """Route delivery failure."""

class AdapterError(OmniError):
    """Adapter-level failure."""

class CancelError(OmniError):
    """Execution was cancelled."""