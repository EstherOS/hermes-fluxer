# Schema reference

The engine accepts a configuration dict at session creation. When run under Hermes, this dict is injected from the `omni:` key in the Hermes config. When standalone, it comes from an `omnimaker.yaml` file.

```yaml
default: <profile-name>                    # optional
profiles:
  <profile-name>:
    nodes: {}
    routes: []
```

## `nodes`

Each node declares its adapter reference, endpoint capabilities, concurrency policy, invocation routes (tools), and opaque config.

```yaml
nodes:
  <name>:
    use: <adapter-ref>           # opaque — resolved by the adapter registry

    # Execution contract
    session: ephemeral | persistent

    # Endpoint capability declaration (documentation + validation)
    consumes:
      - <endpoint>
    produces:
      - <port-name>

    # Concurrency defaults (can be overridden per-route)
    concurrency:
      interrupt_on:
        - <endpoint>
      on_interrupt: replace | queue | drop | parallel

    # Invocation routes — compiled to request/response edges
    tools:
      - name: <tool-name>
        description: <string>               # injected into tool definition
        route_to: <node-name>
        await: sync | async
        response:
          to: <node>.<port>
          as: tool_result | input | interrupt_signal

    # Adapter-specific config — passed through, never interpreted
    config: {}
```

### `use`

An opaque adapter reference resolved by the adapter registry. The engine does not inspect or interpret this value — it instantiates the adapter and passes `config` to it.

Examples:
- `local/crispasr` — file-based ASR
- `local/piper` — Piper TTS
- `hermes/session` — Hermes agent session with full tool access
- `model/llm` — bare model call without tools

### `session`

Controls whether the node maintains state across invocations or creates a fresh context each time.

| Value | Meaning |
|-------|---------|
| `ephemeral` | New invocation gets a fresh session. No state carries over. |
| `persistent` | Node keeps its context across invocations within a conversation. |

### `concurrency`

Default arrival policy for this node, applied to every inbound route unless the route provides its own `transport.concurrency` override. The policy concerns mid-execution arrivals only — while the node is idle, an arrival always runs immediately.

```yaml
concurrency:
  interrupt_on:
    - <endpoint>                    # sources whose arrivals may interrupt (empty = any)
  on_interrupt: replace | queue | drop | parallel
```

| `on_interrupt` | Behavior when an interrupt-eligible arrival lands mid-execution |
|----------------|------------------------------------------------------------------|
| `queue` | The arrival waits in the node's input queue for its turn (default). |
| `replace` | Abort the current execution and begin the arrival immediately. |
| `drop` | Discard the arrival; the current execution continues untouched. |
| `parallel` | Begin a concurrent execution; the current one is untouched. |

`interrupt_on` scopes which sources may interrupt. Entries match the envelope's source — the full `node.port` string or the originating node name. Arrivals from sources not listed fall back to `queue`; nothing is discarded unless `drop` applies to a triggering source.

### `tools`

Defines invocation routes — edges where the source node makes a request/response call to another node. These are the engine's primitive for agent delegation, sub-agent handoffs, and async reasoning.

```yaml
tools:
  - name: <name>            # tool name the source node uses in its tool call
    description: <string>   # optional — injected into the tool definition
    route_to: <node-name>   # which node receives the request
    await: sync | async
    response:
      to: <node>.<port>     # where the response envelope lands
      as: tool_result | input | interrupt_signal
```

| `await` | Meaning |
|---------|---------|
| `sync` | Reserved — not implemented. Use `async`. |
| `async` | Source node continues producing while the target works. The response is delivered as an independent envelope at `response.to` when the target's execution completes — the target's outputs are merged into a single response envelope first. |

| `as` | How the response envelope is injected at the target |
|------|------------------------------------------------------|
| `tool_result` | Injected into the target node's next context as if a tool call just returned. |
| `input` | Appears as an ordinary message to the target. |
| `interrupt_signal` | The response is interrupt-eligible on the target — it can preempt current work per the target's `concurrency` policy. |

The node's tool list is provided to its adapter (`bind_tools`) so it can present the tools to its model. Tool calls are emitted as `tool_call` envelopes and executed by the engine through these invocation routes — the adapter never routes or executes graph tools itself.

### `config`

Opaque object passed verbatim to the adapter's `open()` method. The engine never inspects its contents. What goes here is entirely adapter-defined — model parameters, profile names, file paths, API keys, etc.

### Engine-level fields that do NOT exist

Rejected to maintain engine agnosticism:

- No `harness:` field — harness access is an adapter concern, configured inside `config:`.
- No `bindings:` block — binding registration is done at the adapter registry level, not in the profile YAML.
- No node execution-mode key — a node executes the way its adapter executes; its delivery behavior is shaped by `session:`, `concurrency:`, and route `transport:`.

## `streams`

Named external sources. A stream entry binds a graph name to a platform endpoint; a node that lists the name in `consumes:` receives envelopes fed from that source (compiled to an implicit route from the endpoint).

```yaml
streams:
  mic:
    source: "@fluxer.audio"
```

Streams are naming for readability — delivery semantics live on routes (`transport:`), not here.

## `routes`

Dataflow and invocation edges between endpoints. The route list is the authoritative topology of the profile.

```yaml
routes:
  - from: <endpoint>
    to: <endpoint>

    # Structured selector on envelope metadata
    when:
      type: <envelope-type>

    # Override the receiving node's concurrency defaults
    transport:
      concurrency:
        mode: queue | replace | drop | parallel
```

### `from` / `to`

Endpoint syntax:

| Form | Meaning |
|------|---------|
| `@<binding>.<port>` | External endpoint — platform or harness adapter |
| `<node>.<port>` | Node-local endpoint port |

Platform endpoints are referenced directly as `@binding.port`. The `streams:` block can name frequently used sources; both forms compile to the same routes.

### `when`

Structured selector evaluated against the envelope's metadata. Only envelopes matching the selector are delivered through the route.

```yaml
when:
  type: text/final           # only deliver final transcripts
```

The selector matches one level deep against `envelope.type`.

### `transport`

Overrides the receiving node's default concurrency policy for this specific route. When absent, the node's `concurrency` block applies.

| `concurrency.mode` | Behavior when new envelope arrives while destination is busy |
|---------------------|-------------------------------------------------------------|
| `queue` | Serialize — envelope waits in the node's input queue for its turn. |
| `replace` | Cancel current execution, begin processing the new envelope immediately. |
| `drop` | Discard the new envelope if the destination is busy. |
| `parallel` | Start a new concurrent execution on the destination. |

### Invocation routes in the route list

Invocation routes can also appear directly in `routes:` rather than only in `tools:` blocks.

```yaml
routes:
  - invoke: <endpoint>
    to: <node>
    await: async
    response:
      to: <node>.<port>
      as: tool_result
```

## Control endpoints

Reserved endpoints the engine executes on delivery. They are addressed like any other endpoint but are never handed to the adapter — the envelope is not data, it is an operation on the node's execution.

| Endpoint | Delivery effect |
|----------|-----------------|
| `<node>.cancel` | Cancel the node's active executions and drop its queued, not-yet-started arrivals. No-op when idle. The adapter's cleanup runs; nothing is delivered to `accept()`. |

Barge-in and stops are expressed as wiring:

```yaml
routes:
  - from: ears.speech
    to: talker.cancel
  - from: ears.speech
    to: mouth.cancel
```

No platform component holds a special interrupt path: the event that should stop work (speech onset, a supervisor signal, a hang-up) is routed at whatever should stop.

Adapter-facing control is a different mechanism: ordinary envelopes of type `control` route like any envelope and are interpreted by the receiving adapter (opaque payload — soften, regenerate, flush, whatever its contract says). Control endpoints are engine-executed; `control` envelopes are adapter-executed.

## Endpoint syntax summary

| Example | Meaning |
|---------|---------|
| `@fluxer.audio` | Platform binding `fluxer`, port `audio` |
| `@fluxer.audio_out` | Platform binding `fluxer`, port `audio_out` |
| `ears.transcript` | Node `ears`, port `transcript` |
| `talker.cancel` | Reserved control endpoint — see Control endpoints |
| `@fluxer.events` | Platform event bus |