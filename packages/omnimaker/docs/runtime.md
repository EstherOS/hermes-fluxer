# Runtime

The runtime owns graph execution, envelope routing, lifecycle, and concurrency control.

## Internal layers

```
YAML profile loader
        ↓
Graph compiler / validator
        ↓
Runtime router
        ↓
Execution / cancellation
        ↓
Adapter boundary
```

### Profile loader

Reads the configuration dict (injected by the host or from file), resolves `use:` references against the adapter registry.

### Graph compiler

Resolves nodes, endpoints, and routes. Validates capability compatibility — source output type matches destination input type — without knowing what any node "means."

### Runtime router

Moves envelopes between endpoints according to routes. Handles:
- Dataflow edges: every incoming envelope is pushed to matching destinations
- Invocation edges: request/response lifecycle with await semantics
- External endpoints: delivery to binding adapters

### Execution layer

Owns tasks, streams, cancellation, backpressure, and correlation. Every execution runs as a tracked task with a unique ID and parent tracking; cancelling an execution closes the adapter's output generator, so cleanup runs (HTTP streams close, subprocesses die).

### Adapter boundary

Abstract interface that external implementations satisfy. Adapters declare their endpoint contracts at registration time; the engine validates compatibility at compile time.

## Interruption

Two engine-side mechanisms, answering two different questions:

**Arrival policy — what happens to the new envelope?** A new envelope reaching a busy node follows the node's `concurrency` policy (or the route's `transport` override): queue, replace, drop, or parallel. The engine performs the outcome — a queued envelope is not shown to the adapter until it runs.

**Control delivery — stop this node's work.** A `<node>.cancel` endpoint routed from an event: on delivery the engine cancels the node's active executions and drops its queued arrivals. No replacement input required.

Cancellation raises into the adapter at its current await point — the generator closes and cleanup runs. Because every execution carries its `turn_id`, cancellation scopes to a turn: every execution carrying that turn id, on any node, is cancelled together (execution correlation runs through the envelope chain).

## Queues

Each node has an ordered backlog. Envelopes land there when the arrival policy is `queue`; they run in order as the node goes idle. A `<node>.cancel` delivery purges the backlog along with active executions. The backlog is bounded — overflow drops the newest arrival with a warning.

## Invocations

A `tools:` entry compiles to an invocation route from the source node's tool-call endpoint (`<node>.tool_call`) to the target. The source adapter receives the node's tool list via `bind_tools()` and emits `tool_call` envelopes; the engine delivers them.

- `await: sync` — Reserved — not implemented.
- `await: async` — the target runs independently; when its execution completes, its outputs are merged into one response envelope and delivered to `response.to` with the `as:` type.

Either way the source adapter never routes anything itself — graph tools are engine-delivered like every other envelope.

## Session lifecycle

Sessions scope node instances, queues, buffers, cancellation domains, and traces to one external interaction (e.g., a Fluxer voice call). When the call ends, the session stops, which tears down all nodes.