"""Profile compiler — YAML config to runtime graph."""

from __future__ import annotations

from omnimaker.types import (
    ConcurrencyConfig,
    Endpoint,
    NodeConfig,
    ProfileConfig,
    ProfileError,
    Route,
    StreamConfig,
    ToolDef,
    TransportConfig,
)


def compile_profile(name: str, raw: dict) -> ProfileConfig:
    """Compile a raw YAML dict into a validated ProfileConfig.

    *raw* is the content of ``omni.profiles.<name>`` from the config file.
    """
    profile = ProfileConfig(name=name)

    # Streams
    for s_name, s_raw in raw.get("streams", {}).items():
        if not isinstance(s_raw, dict):
            raise ProfileError(f"stream '{s_name}' must be a mapping")
        profile.streams[s_name] = StreamConfig(
            name=s_name,
            type=s_raw.get("type", ""),
            source=s_raw.get("source", ""),
            optional=s_raw.get("optional", False),
        )

    # Nodes
    for n_name, n_raw in raw.get("nodes", {}).items():
        if not isinstance(n_raw, dict):
            raise ProfileError(f"node '{n_name}' must be a mapping")
        node = NodeConfig(name=n_name, use=n_raw.get("use", ""))
        if not node.use:
            raise ProfileError(f"node '{n_name}' is missing 'use:' field")

        node.config = n_raw.get("config", {})
        node.session = n_raw.get("session", "ephemeral")
        if node.session not in ("ephemeral", "persistent"):
            raise ProfileError(
                f"node '{n_name}': invalid session '{node.session}' "
                f"(expected ephemeral | persistent)"
            )
        node.consumes = n_raw.get("consumes", [])
        node.produces = n_raw.get("produces", [])

        # Concurrency
        conc_raw = n_raw.get("concurrency", {})
        if conc_raw:
            on_int = conc_raw.get("on_interrupt", "queue")
            if on_int not in ("replace", "queue", "drop", "parallel"):
                raise ProfileError(
                    f"node '{n_name}': invalid on_interrupt '{on_int}' "
                    f"(expected replace | queue | drop | parallel)"
                )
            node.concurrency = ConcurrencyConfig(
                interrupt_on=conc_raw.get("interrupt_on", []),
                on_interrupt=on_int,
            )

        # Tools
        for t_raw in n_raw.get("tools", []):
            if not isinstance(t_raw, dict):
                raise ProfileError(f"tool in node '{n_name}' must be a mapping")
            tool = ToolDef(
                name=t_raw.get("name", ""),
                route_to=t_raw.get("route_to", ""),
                description=t_raw.get("description", ""),
                await_mode=t_raw.get("await", "async"),
                response_as=t_raw.get("response", {}).get("as", "tool_result"),
            )
            resp_to = t_raw.get("response", {}).get("to")
            if resp_to:
                tool.response_to = resp_to
            node.tools.append(tool)

        profile.nodes[n_name] = node

    # Routes
    for r_raw in raw.get("routes", []):
        if not isinstance(r_raw, dict):
            raise ProfileError("each route must be a mapping")

        # Determine kind
        is_invoke = "invoke" in r_raw
        source_str = r_raw.get("invoke") if is_invoke else r_raw.get("from", "")
        dest_str = r_raw.get("to", "")

        if not source_str or not dest_str:
            raise ProfileError("route missing 'from' (or 'invoke') and 'to'")

        route = Route(
            source=Endpoint.parse(source_str),
            dest=Endpoint.parse(dest_str),
            kind="invocation" if is_invoke else "dataflow",
            when=r_raw.get("when"),
        )

        # Transport
        t_raw = r_raw.get("transport", {})
        if t_raw:
            route.transport = TransportConfig(
                concurrency=t_raw.get("concurrency"),
            )

        # Invocation fields
        if is_invoke or r_raw.get("await"):
            route.await_mode = r_raw.get("await", "sync")
            resp_raw = r_raw.get("response") or {}
            resp_to = resp_raw.get("to")
            if resp_to:
                route.response_to = Endpoint.parse(resp_to)
            route.response_as = resp_raw.get("as", "tool_result")

        profile.routes.append(route)

    # Expand tool definitions into routes
    _expand_tools(profile)

    # Resolve stream references in nodes (stream names -> endpoints)
    _resolve_streams(profile)

    return profile


def _expand_tools(profile: ProfileConfig) -> None:
    """Convert each tool definition on a node into an invocation route."""
    for node_name, node in profile.nodes.items():
        for tool in node.tools:
            if not tool.route_to:
                raise ProfileError(
                    f"tool '{tool.name}' on node '{node_name}' missing route_to"
                )

            # route_to is a node name — default port is "input"
            target_node = tool.route_to
            if "." in target_node:
                dest_ep = Endpoint.parse(target_node)
            else:
                dest_ep = Endpoint.parse(f"{target_node}.input")

            # Build response destination endpoint
            resp_endpoint: Endpoint | None = None
            if tool.response_to:
                resp_endpoint = Endpoint.parse(tool.response_to)
            else:
                # Default: response goes back to the source node's tool_result port
                resp_endpoint = Endpoint.parse(f"{node_name}.tool_result")

            route = Route(
                source=Endpoint.parse(f"{node_name}.tool_call"),
                dest=dest_ep,
                kind="invocation",
                await_mode=tool.await_mode,
                response_to=resp_endpoint,
                response_as=tool.response_as,
            )
            profile.routes.append(route)

            # For sync invocations, also add a return dataflow route
            # from the target's main output port to the response destination
            if tool.await_mode == "sync":
                return_route = Route(
                    source=Endpoint(node=target_node, port="output"),
                    dest=resp_endpoint,
                    kind="dataflow",
                )
                profile.routes.append(return_route)

        # Keep node.tools — the engine provisions them to the adapter via
        # bind_tools(); the compiled routes carry the delivery mechanics.


def _resolve_streams(profile: ProfileConfig) -> None:
    """Resolve stream-name references in node ``consumes:`` to their
    ``@binding.port`` endpoint equivalents, and add feeder routes for each."""
    # Build stream-name -> endpoint lookup
    stream_map: dict[str, Endpoint] = {}
    for s_name, s_cfg in profile.streams.items():
        stream_map[s_name] = Endpoint.parse(s_cfg.source)

    # Add dataflow routes from stream sources to nodes that list them
    # This is the "auto-wiring shorthand" — explicit routes override.
    for node_name, node in profile.nodes.items():
        for consumed in node.consumes:
            # consumed may be a stream name or "node.port"
            if consumed in stream_map:
                stream_ep = stream_map[consumed]
                dest_ep = Endpoint.parse(f"{node_name}.input")
                # Only add if no explicit route already exists
                already_wired = any(
                    r.source == stream_ep and r.dest == dest_ep
                    for r in profile.routes
                )
                if not already_wired:
                    profile.routes.append(Route(
                        source=stream_ep,
                        dest=dest_ep,
                        kind="dataflow",
                    ))