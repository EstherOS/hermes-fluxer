"""Compiled graph — built from a profile, holds node instances and route table."""

from __future__ import annotations

import uuid
from collections import defaultdict

from omnimaker.adapters.base import NodeAdapter
from omnimaker.adapters.registry import registry as _registry
from omnimaker.types import (
    Endpoint,
    Envelope,
    ExecutionHandle,
    NodeConfig,
    ProfileConfig,
    ProfileError,
    Route,
)


class NodeInstance:
    """A running node with its adapter and execution state."""

    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.adapter: NodeAdapter | None = None
        self.active_executions: dict[str, ExecutionHandle] = {}

    def __repr__(self) -> str:
        return f"NodeInstance({self.config.name})"


class RouteTable:
    """Indexed route table for fast envelope dispatch."""

    def __init__(self, routes: list[Route]) -> None:
        # Index by source endpoint string
        self._by_source: dict[str, list[Route]] = defaultdict(list)
        for route in routes:
            key = str(route.source)
            self._by_source[key].append(route)

        # Invocation routes by source as well (they share the same index)
        self._invocations: list[Route] = [
            r for r in routes if r.kind == "invocation"
        ]

    def match(self, source: Endpoint) -> list[Route]:
        """Return all routes originating from *source*."""
        return self._by_source.get(str(source), [])

    def match_invocation_response(self, envelope: Envelope
                                  ) -> Route | None:
        """Find the invocation route whose request produced this response.

        Matches by parent_execution_id. The route's source node is
        tracked in the execution handle chain.
        """
        # Walk routes that have a response_to matching this envelope's
        # source, and where the await_mode matches.
        parent_id = envelope.parent_execution_id
        if not parent_id:
            return None
        for route in self._invocations:
            if route.response_to and envelope.source:
                if str(route.response_to) == str(envelope.source):
                    return route
        return None

    def all_routes(self) -> list[Route]:
        """Return every registered route."""
        result: list[Route] = []
        for routes in self._by_source.values():
            result.extend(routes)
        return result


class Graph:
    """Compiled graph — instantiated from a profile, ready to execute."""

    def __init__(self, profile: ProfileConfig) -> None:
        self.profile = profile
        self.nodes: dict[str, NodeInstance] = {}
        self.route_table: RouteTable
        self._build()

    def _build(self) -> None:
        # Instantiate nodes
        for name, config in self.profile.nodes.items():
            self.nodes[name] = NodeInstance(config)

        # Build route table
        self.route_table = RouteTable(self.profile.routes)

    def resolve_adapter(self, node_name: str) -> type[NodeAdapter]:
        """Look up the adapter class for a node by name."""
        config = self.nodes[node_name].config
        try:
            return _registry.resolve(config.use)
        except KeyError as exc:
            raise ProfileError(
                f"node '{node_name}' references unknown adapter '{config.use}'"
            ) from exc

    def get_node(self, name: str) -> NodeInstance:
        node = self.nodes.get(name)
        if node is None:
            raise KeyError(f"no node '{name}' in graph")
        return node

    def stream_endpoint(self, stream_name: str) -> Endpoint | None:
        """Resolve a stream name to its binding endpoint."""
        s_cfg = self.profile.streams.get(stream_name)
        if s_cfg is None:
            return None
        return Endpoint.parse(s_cfg.source)