# Omnimaker-Hermes — Hermes agent adapters for Omnimaker

This package provides adapters that wire Omnimaker nodes into Hermes agent sessions. It depends on both `omnimaker` (for the graph runtime) and the Hermes agent library (for session creation and tool execution).

## Adapters

### `hermes/session`

Wraps a Hermes agent conversation as an omni node. Text in, text out. Answer fragments stream back as they form, so a downstream mouth can start speaking early. Tool rounds run inline through the Hermes tool stack.

The session carries the agent's identity by default: the base system prompt is the agent's SOUL.md (read through the Hermes prompt stack), with `config.system_prompt` appended as node-level instructions. Set `identity: false` for a pure role node (a talker persona, a character).

```yaml
talker:
  use: hermes/session
  session: persistent
  consumes: [ears.transcript]
  produces: [text, tool_call]
  config:
    base_url: http://127.0.0.1:8085
    model: minicpm5-2b
    temperature: 0.4
    max_tokens: 320
    tools: []              # Hermes-tool allow-list — [] = none, omit = all
    identity: false
    system_prompt: "You are the talker — chat briefly; hand deeper questions off."
```

| Config key | Meaning |
|------------|---------|
| `base_url` | OpenAI-compatible endpoint. `/v1`-suffixed bases are accepted. |
| `model` | Model id sent with each request. |
| `api_key` / `api_key_env` | Literal key, or the env var to read it from. Sent as `Authorization: Bearer`. |
| `tools` | Allow-list of Hermes tool names. `[]` = no tools; omitted = all. |
| `identity` | Include the agent identity as the base prompt (default `true`). |
| `system_prompt` | Node instructions, appended after the base prompt. |
| `thinking` | Reasoning on/off (llama-server template flag; default on). |
| `prewarm` | Warm the server prompt cache at open (default on; skip for remote APIs). |
| `temperature`, `max_tokens`, `timeout_seconds` | Model-call parameters. |

**Graph tools.** A node's `tools:` block is provisioned to this adapter by the engine (`bind_tools`). The definitions are appended to the model's tool list; when the model calls one, the adapter emits a `tool_call` envelope and the engine routes it through the compiled invocation route. The node must declare `tool_call` among its `produces:` ports. An async invocation's response arrives later as a `tool_result` envelope (at the configured `response.to`), which the adapter feeds back to the model as the matching tool message; the next model turn voices the result. One hand-off per turn.

### `hermes/harness`

A dedicated harness node: accepts `tool_call` envelopes, executes them through the Hermes tool stack, and returns `tool_result` envelopes. Useful when tool execution should be its own graph participant.

```yaml
nodes:
  harness:
    use: hermes/harness
routes:
  - from: thinker.tool_call
    to: harness.tool_call
  - from: harness.tool_result
    to: thinker.context
```

Registration is consumer-side: a plugin registers the adapter under this reference to make it available to profiles.

## Tool scoping

Tool access is per node. `config.tools` is a Hermes-tool allow-list (`[]` = none, omitted = all), and graph tools come from the node's `tools:` block, executed by the engine through invocation routes. The engine never inspects or restricts tool definitions.
