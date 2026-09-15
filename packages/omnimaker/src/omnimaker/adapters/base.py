"""Abstract base class for omni node adapters."""

from __future__ import annotations

import abc
from typing import AsyncGenerator

from omnimaker.types import Envelope, ExecutionHandle, SessionScope


class NodeAdapter(abc.ABC):
    """Interface between the omni engine and a node implementation.

    Subclasses override the capability class variables and implement
    ``accept()``, ``invoke()``, ``cancel()``, and ``close()``.
    """

    # Capability declarations (overridden by subclasses)
    accepts: dict[str, list[str]] = {}   # {port_name: [envelope_type, ...]}
    emits: dict[str, list[str]] = {}     # {port_name: [envelope_type, ...]}
    session_scope: SessionScope = "ephemeral"

    # Lifecycle

    async def open(self, session_id: str, config: dict) -> None:
        """Called when a persistent node is first used in a session.

        The default is a no-op. Override for one-time setup (load model,
        start subprocess, allocate resources).
        """

    async def close(self) -> None:
        """Called when the session ends or the node is released.

        The default is a no-op. Override to release resources.
        """

    # Execution

    @abc.abstractmethod
    async def accept(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        """Process a dataflow envelope.

        Called for every dataflow route delivery to this node.
        Yields zero or more output envelopes.

        The *handle* can be polled (``handle.cancelled``) to check
        whether the engine has cancelled this execution.
        """
        if False:
            yield

    async def invoke(self, envelope: Envelope,
                     handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]:
        """Process an invocation-route request.

        Called for invocation-route deliveries. The default collects
        ``accept()``'s outputs and merges text outputs into a single
        response envelope (joined with spaces); non-text outputs pass
        through individually. Override when the adapter needs to
        distinguish request/response calls from streaming data.
        """
        if False:
            yield
        outputs: list[Envelope] = []
        async for output in self.accept(envelope, handle):
            outputs.append(output)
        if not outputs:
            return
        if all(isinstance(o.payload, str) for o in outputs):
            merged = outputs[-1]
            merged.payload = " ".join(o.payload for o in outputs)
            yield merged
        else:
            for output in outputs:
                yield output

    async def cancel(self, execution_id: str) -> None:
        """Cancel an active execution.

        Called when the engine cancels this node's execution (replace
        concurrency, session stop, interrupt). The adapter should stop
        producing output and clean up in-progress work.

        The default is a no-op. Override when the adapter holds
        long-running work (model inference, subprocess).
        """

    # Introspection

    def accepts_type_on(self, port: str, env_type: str) -> bool:
        """Check whether this adapter accepts *env_type* on *port*."""
        accepted = self.accepts.get(port, [])
        return env_type in accepted or not accepted  # empty = accept all

    def check_compatibility(self, produces_type: str,
                            produces_port: str, dest_port: str) -> bool:
        """Check if a source producing *produces_type* on *produces_port*
        is compatible with this adapter's *dest_port*."""
        accepted = self.accepts.get(dest_port, [])
        if not accepted:
            return True  # accept-everything port
        return produces_type in accepted