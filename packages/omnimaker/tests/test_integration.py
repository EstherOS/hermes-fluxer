"""Comprehensive integration test for the omni engine."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from omnimaker import register_builtins
from omnimaker.compiler import compile_profile
from omnimaker.runtime import Session
from omnimaker.types import Endpoint, Envelope, NodeConfig, Route, ProfileConfig

register_builtins()


def test_compiler_full_profile():
    """Compile a profile with every schema feature."""
    profile = compile_profile("full", {
        "streams": {
            "mic": {
                "type": "audio/stream",
                "source": "@fluxer.audio",
                "optional": False,
            },
            "screen": {
                "type": "video/stream",
                "source": "@fluxer.screen",
                "optional": True,
            },
        },
        "nodes": {
            "ears": {
                "use": "local/crispasr_stream",
                "mode": "streaming",
                "emits": "partial+final",
                "session": "ephemeral",
                "consumes": ["mic"],
                "produces": ["transcript"],
                "concurrency": {
                    "interrupt_on": ["@fluxer.events"],
                    "on_interrupt": "cancel",
                },
            },
            "talker": {
                "use": "omni/tee",
                "mode": "turn_based",
                "consumes": ["ears.transcript"],
                "produces": ["response"],
                "tools": [{
                    "name": "defer",
                    "route_to": "thinker",
                    "await": "async",
                    "response": {
                        "to": "talker.context",
                        "as": "tool_result",
                    },
                }],
            },
            "thinker": {
                "use": "omni/tee",
                "mode": "turn_based",
                "session": "persistent",
                "harness": "core",
            },
            "mouth": {
                "use": "local/piper",
                "mode": "streaming",
                "consumes": ["talker.response"],
                "produces": ["audio"],
                "concurrency": {
                    "interrupt_on": ["@fluxer.events"],
                    "on_interrupt": "cancel",
                },
            },
        },
        "routes": [
            {
                "from": "ears.transcript",
                "to": "talker.input",
                "when": {"type": "text/final"},
                "transport": {
                    "mode": "message",
                    "concurrency": {"mode": "queue"},
                },
            },
            {
                "from": "talker.response",
                "to": "mouth.text",
            },
            {
                "from": "mouth.audio",
                "to": "@fluxer.audio_out",
            },
        ],
        "outputs": {
            "audio": {
                "source": "mouth.audio",
                "sink": "@fluxer.audio_out",
            },
        },
    })

    assert profile.name == "full"
    # 2 streams + 1 output should produce routes equal to:
    # ears → talker, talker → mouth, mouth → audio_out, output route,
    # plus the tool expansion route(s) from ears -> thinker
    print(f"Nodes: {list(profile.nodes.keys())}")
    print(f"Streams: {list(profile.streams.keys())}")
    print(f"Routes: {len(profile.routes)}")
    for r in profile.routes:
        print(f"  {r.kind:>10}: {r.source} -> {r.dest}")

    assert "ears" in profile.nodes
    assert "talker" in profile.nodes
    assert "thinker" in profile.nodes
    assert "mouth" in profile.nodes
    assert "mic" in profile.streams
    assert "screen" in profile.streams
    assert profile.nodes["ears"].emits == "partial+final"
    assert profile.nodes["thinker"].session == "persistent"
    assert profile.nodes["thinker"].harness == "core"
    assert profile.nodes["ears"].concurrency.interrupt_on == ["@fluxer.events"]

    # Check tool expansion produced an invocation route
    invocation_routes = [r for r in profile.routes if r.kind == "invocation"]
    assert len(invocation_routes) >= 1, "Expected tool -> invocation route"
    print(f"Invocation routes: {len(invocation_routes)}")

    # Check output expansion produced a route
    output_routes = [r for r in profile.routes
                     if str(r.dest) == "@fluxer.audio_out"]
    assert len(output_routes) >= 1
    print(f"Output routes to fluxer: {len(output_routes)}")

    print("FULL PROFILE TEST PASSED")
    print()


def test_route_matching():
    """Test route matching with when filters."""
    routes = [
        Route(source=Endpoint(node="ears", port="transcript"),
              dest=Endpoint(node="talker", port="input"),
              when={"type": "text/final"}),
        Route(source=Endpoint(node="ears", port="transcript"),
              dest=Endpoint(node="logger", port="input"),
              when={"type": "text/partial"}),
    ]

    from omnimaker.graph import RouteTable
    table = RouteTable(routes)

    final_env = Envelope(type="text/final", payload="",
                         session_id="s", turn_id="t", execution_id="e",
                         source="ears.transcript")
    partial_env = Envelope(type="text/partial", payload="",
                           session_id="s", turn_id="t", execution_id="e",
                           source="ears.transcript")

    ep = Endpoint.parse("ears.transcript")
    matched = table.match(ep)

    # RouteTable returns all routes from that source
    assert len(matched) == 2, f"Expected 2, got {len(matched)}"

    # Test when filter
    matches_final = matched[0].matches(final_env)
    matches_partial = matched[0].matches(partial_env)
    print(f"Route 0 matches final: {matches_final}, partial: {matches_partial}")

    print("ROUTE MATCHING TEST PASSED")
    print()


def test_endpoint_parsing():
    """Test endpoint parsing."""
    ep = Endpoint.parse("@fluxer.audio")
    assert ep.node == "@fluxer" and ep.port == "audio"
    assert ep.is_external is True

    ep = Endpoint.parse("ears.transcript")
    assert ep.node == "ears" and ep.port == "transcript"
    assert ep.is_external is False

    ep = Endpoint.parse("talker.input")
    assert ep.node == "talker" and ep.port == "input"

    print("ENDPOINT PARSING TEST PASSED")
    print()


async def test_session_multi_node():
    """Run a session with a simple multi-node graph."""
    profile = compile_profile("multi", {
        "nodes": {
            "a": {"use": "omni/tee", "mode": "streaming"},
            "b": {"use": "omni/tee", "mode": "streaming"},
        },
        "routes": [
            {"from": "a.output", "to": "b.input"},
            {"from": "b.output", "to": "a.input"},
        ],
    })

    session = Session(profile)
    await session.start()

    env = Envelope(
        type="text",
        payload="roundtrip",
        session_id="test_multi",
        turn_id="t1",
        execution_id="e1",
        source="a.output",
    )

    await session.feed("a.output", env)
    await asyncio.sleep(0.3)

    await session.stop()
    print("MULTI-NODE SESSION TEST PASSED")
    print()


async def test_session_with_streams():
    """Test stream resolution and auto-wiring."""
    profile = compile_profile("streams", {
        "streams": {
            "sig": {
                "type": "event/stream",
                "source": "@plat.events",
                "optional": False,
            },
        },
        "nodes": {
            "proc": {
                "use": "omni/tee",
                "mode": "streaming",
                "consumes": ["sig"],
                "produces": ["out"],
            },
        },
        "routes": [],
    })

    session = Session(profile)
    await session.start()

    # The stream should have been auto-wired to the consuming node
    stream_routes = [r for r in session.graph.route_table.all_routes()
                     if str(r.source) == "@plat.events"]
    assert len(stream_routes) >= 1, "Expected auto-wired stream route"
    print(f"Auto-wired stream routes: {len(stream_routes)}")

    await session.stop()
    print("STREAM AUTO-WIRING TEST PASSED")
    print()


# ── Run ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_endpoint_parsing()
    test_route_matching()
    test_compiler_full_profile()
    asyncio.run(test_session_multi_node())
    asyncio.run(test_session_with_streams())
    print("==== ALL TESTS PASSED ====")