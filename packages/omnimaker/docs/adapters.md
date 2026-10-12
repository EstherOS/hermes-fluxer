# Adapters

Adapters are the engine's only mechanism for extending behavior. Every node in the graph is backed by an adapter, registered under a `use:` name in the adapter registry.

## Adapter interface

```python
class NodeAdapter:
    accepts: dict[str, list[str]]      # {port: [envelope_type, ...]} what the node can receive
    emits: dict[str, list[str]]        # {port: [envelope_type, ...]} what the node can produce
    session_scope: str                 # "ephemeral" | "persistent"

    async def open(self, session_id: str, config: dict) -> None: ...
    def bind_tools(self, tools: list[dict]) -> None: ...
    async def accept(self, envelope: Envelope, handle: ExecutionHandle) -> AsyncGenerator[Envelope, None]: ...
    async def cancel(self, execution_id: str) -> None: ...
    async def close(self) -> None: ...
```

- `open()` — called when the session starts. Receives the opaque `config:` dict.
- `bind_tools()` — called after `open()` with the node's declared graph tools (`{name, description, await}`), when the profile declares any. The adapter may present them to its model; calls are emitted as `tool_call` envelopes and the engine executes the route.
- `accept()` — called for each incoming envelope. Yields output envelopes back to the engine for routing.
- `cancel()` — called when a concurrent execution is replaced, control-cancelled (`<node>.cancel`), or the session stops.
- `close()` — called when the session ends.

Output envelopes may carry `metadata["port"]` naming one of the node's declared `produces:` ports; untagged outputs fan out to all declared ports.

## Registration

Adapters are registered via the registry:

```python
from omnimaker.adapters.registry import registry
registry.register("local/piper", PiperTTSBackend)
registry.register_binding("fluxer", FluxerBinding)
```

The engine resolves `use:` against the registry at profile compile time.

## Binding adapters

Binding adapters expose external endpoints under the `@<name>.*` namespace. They are platform-specific — a Fluxer binding exposes `@fluxer.audio`, `@fluxer.audio_out`, etc. The engine routes envelopes to binding adapters when a route's destination begins with `@`.

## What adapters own

- Model invocation, API calls, file I/O, audio processing
- Harness/tool execution (if the adapter wraps Hermes agent session)
- Internal conversation state (if `session_scope: persistent`)

## What adapters do not own

- Routing — the engine decides where output envelopes go, based on `routes:`
- Delivery-boundary concurrency — the engine queues, replaces, drops, and cancels executions; inside a live session the adapter owns its own message handling (a harness's internal queue/interrupt semantics are the harness's)
- Session lifecycle — the engine manages session creation and teardown