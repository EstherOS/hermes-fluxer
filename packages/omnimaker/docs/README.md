# Omnimaler — streaming message graph engine

Omnimaler is a **streaming message graph runtime**. It wires opaque nodes together into a directed graph and moves typed envelopes between those nodes according to configurable transport semantics. The engine does not know what a "talker" or "thinker" is — those are properties of the graph the author built, not concepts the engine recognizes.

## Core primitives

| Primitive | Description |
|-----------|-------------|
| **Node** | An opaque processing participant. Has a `use:` reference that resolves to an adapter, endpoint ports, optional invocation routes (tools), and opaque config. |
| **Endpoint** | A named I/O port on a node or binding (`node.port` or `@binding.port`). |
| **Route** | A directed edge from one endpoint to another. Two kinds: **dataflow** (continuous push) and **invocation** (request/response with lifecycle). |
| **Envelope** | A typed message unit with `type`, `payload`, session/turn/invocation correlation, and metadata. Everything moving through the graph is an envelope. |
| **Session** | A scope that binds node instances, queues, buffers, cancellation domains, and traces to one external interaction (e.g. a Fluxer voice call). |

## Two route kinds

The engine has two execution primitives that compile to the same internal transport substrate.

**Dataflow route:** whenever an envelope arrives at the source endpoint, push it to every matching destination. Used for continuous streams (audio frames, partial transcripts) and event delivery.

```
ears.transcript ──→ brain.input
```

**Invocation route:** the source node makes a request to a target node; the response is delivered to a specified endpoint and type when the callee finishes. Used for agent delegation, sub-agent calls, and async deferrals.

```
talker.defer ──→ thinker
                 └── response → talker.context (as tool_result)
```

Both appear as entries in the `routes:` list or — for invocation — as items in a node's `tools:` block.

## What the engine never does

- Pattern-match a graph into a named paradigm (no "Jarvis mode")
- Assign semantic roles to nodes (no built-in "talker", "thinker", "ears")
- Interpret `use:` references (they are opaque adapter handles)
- Interpret `config:` values (they are opaque — the adapter defines its own contract)
- Know what a "harness" is (harness is an adapter, not an engine concept)

## Quick profile shapes

These are not prefab modes. They are simply the graph that results from a particular wiring.

**Cascade (STT → model → TTS):**

```yaml
nodes:
  ears:  { use: local/crispasr }
  brain: { use: hermes/session }
  mouth: { use: local/piper }
routes:
  - from: ears.transcript
    to: brain.input
  - from: brain.text
    to: mouth.text
```

**Delegated reasoning (fast frontend + async backend):**

```yaml
nodes:
  talker:
    use: hermes/session
    tools:
      - name: defer
        route_to: thinker
        await: async
        response:
          to: talker.context
          as: tool_result
    config:
      identity: false
      system_prompt: "You are the talker — chat briefly; hand deeper questions to the thinker."
  thinker:
    use: hermes/session
    session: persistent
```

**Realtime front end (barge-in as wiring):**

```yaml
nodes:
  talker:
    use: hermes/session
    session: persistent
    concurrency:
      on_interrupt: replace     # new speech supersedes the current turn
      interrupt_on: [ears]
  thinker:
    use: hermes/session
    session: persistent
  ears:  { use: local/crispasr }
  mouth: { use: local/piper }
routes:
  - from: "@fluxer.speech"      # platform VAD onset — a bare signal, no payload
    to: talker.cancel           # speech onset stops the current turn...
  - from: "@fluxer.speech"
    to: mouth.cancel            # ...and the mouth
  - from: ears.transcript
    to: talker.input
```

Speech onset is routed at what should stop; the finalized transcript arrives as ordinary input. `interrupt_on` scopes which sources may replace the talker's current turn (user speech yes — worker results queue and weave in). Each track — audio, video, worker results — interrupts and queues independently.