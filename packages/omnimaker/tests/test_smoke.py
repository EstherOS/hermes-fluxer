"""Smoke test for the omni engine core."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from omnimaker import register_builtins
from omnimaker.compiler import compile_profile
from omnimaker.runtime import Session
from omnimaker.types import Endpoint, Envelope

register_builtins()

profile = compile_profile("smoke", {
    "nodes": {
        "passthrough": {
            "use": "omni/tee",
            "mode": "streaming",
        },
    },
    "routes": [
        {"from": "passthrough.output", "to": "passthrough.input"},
    ],
})

assert profile.name == "smoke"
assert "passthrough" in profile.nodes
assert len(profile.routes) == 1
print(f"Profile compiled: {profile.name}, {len(profile.nodes)} nodes, {len(profile.routes)} routes")

graph = Session(profile).graph
assert "passthrough" in graph.nodes
print(f"Graph built: {list(graph.nodes.keys())}")

routes = graph.route_table.match(Endpoint.parse("passthrough.output"))
print(f"Routes matched: {len(routes)}")


async def main():
    session = Session(profile)
    await session.start()

    env = Envelope(
        type="text",
        payload="hello",
        session_id="test",
        turn_id="t1",
        execution_id="e1",
        source="passthrough.output",
    )

    await session.feed("passthrough.output", env)
    await asyncio.sleep(0.2)

    await session.stop()
    print("Session ran successfully")

asyncio.run(main())
print("ALL OK")