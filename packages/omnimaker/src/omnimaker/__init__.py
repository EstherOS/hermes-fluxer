"""hermes-omni — streaming message graph engine (beta 3).

Core primitives: Node, Endpoint, Route, Envelope.
Two route kinds: dataflow (push) and invocation (request/response).
Paradigms emerge from wiring — the engine never pattern-matches a graph.

Usage:

    # 1. Build a profile config manually or load from YAML
    from omnimaker.compiler import compile_profile

    raw = {
        "nodes": {
            "echo": {
                "use": "omni/tee",
                "mode": "streaming",
            }
        },
        "routes": [
            {"from": "echo.output", "to": "@platform.output"},
        ],
    }
    profile = compile_profile("demo", raw)

    # 2. Create a session and feed envelopes
    from omnimaker.runtime import Session

    session = Session(profile)
    await session.start()
"""

from omnimaker.adapters.registry import AdapterRegistry, registry
from omnimaker.compiler import compile_profile
from omnimaker.runtime import Session

# Convenience
register_adapter = registry.register
register_binding = registry.register_binding
register_builtins = registry.register_builtins
resolve_adapter = registry.resolve

__all__ = [
    "AdapterRegistry",
    "Session",
    "compile_profile",
    "register_adapter",
    "register_binding",
    "register_builtins",
    "registry",
    "resolve_adapter",
]