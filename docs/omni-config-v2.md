# Omni Engine Config v2 — Component Grammar

## Design Principle

The engine does **not** pattern-match profiles into prefab pipelines. It reads the component graph as the user declared it, creates push routes between components, and lets data flow naturally. A profile with `ears`, `talker`, `thinker`, and `mouth` behaves exactly as those connections describe — no hidden wiring, no implied stages.

## Concepts

A profile is a **graph of components**. Each component is a model or service with declared inputs, outputs, and optionally tools for other components to call.

**Push routing** (`ins`/`outs`): When a component produces output, it pushes to ALL destinations listed in its `outs` for that sense simultaneously. Destinations receive data automatically — no coordination layer needed.

**Tool routing** (`tools`): Named capabilities a component exposes for on-demand invocation by other components. Unlike push routing (automatic), tools are called when the consumer decides.

**Core**: The component with `harness: core` tool — it has full access to the Hermes agent (tools, context, system prompts). Other components have only the tools explicitly declared in their `tools` section.

### Data flow

Data enters the graph from `user` (mic audio, text channel, camera, screenshare) and flows through components via push routes. Each component processes incoming data and pushes results downstream. The graph replaces the FSM — no state machine coordinates the pipeline.

```
user → ears (audio → text) → talker (processes, may route to thinker)
 → thinker (harness: core, generates response) → mouth (text → audio) → user
```

The bridge reads/writes from the graph endpoints:
- Audio from the call goes to whichever component declares `audio: [user]` in its `ins`
- Audio for the call comes from whichever component declares `audio: [user]` in its `outs`
- Text channel messages go to whichever component declares `text: [user]` in its `ins`

## Grammar

```yaml
omni:
  profiles:
    <name>:
      components:
        <name>:
          ins:                          # what this component receives
            <sense>: [<source>, ...]    # "user" or another component name
          outs:                         # what this component pushes to
            <sense>: [<dest>, ...]      # "user" or another component name
          tools:                        # tools this component exposes
            <tool_name>: <target>       # "core" for full harness, component name for specific
          io:                           # per-connection properties (optional)
            <sense>:
              mode: live | tape | frame
              interrupt: true | false
```

### Sources and destinations

- `user` — the voice call or text channel. Audio/video/image originates from or goes to the call.
- `<component_name>` — output from another component in the same profile.

### Per-connection properties

| property | values | default | description |
|---|---|---|---|
| `mode` | `live`, `tape`, `frame` | `live` for audio, `tape` for video, `frame` for image | How data is captured |
| `interrupt` | `true`, `false` | `true` for audio, `false` for rest | Whether new input cancels current processing |

### Tools

The `core` target is reserved. A component with `tools: {harness: core}` gets full Hermes agent capabilities — it's the brain. Other components only have the tools explicitly listed.

Common tool patterns:
- `harness: core` — full agent access (tools, context, prompts)
- `defer: <name>` — call another component for deep processing
- `image: <name>` — delegate image analysis
- `video: <name>` — delegate video processing (tape capture)

## Engine behavior

The engine builds the component graph from the config. For each component:

1. **Resolves backends**: Each sense in `ins`/`outs` maps to a backend. `text_in` maps to `text_in` backend, `audio_out` maps to `audio_out` backend, etc. Backend names use the existing registry.

2. **Creates push routes**: For every `outs` entry, a `PushRoute(source, sense, [dests])` is created. When the source component produces data of that sense, it's pushed to all dests.

3. **Connects to call**: The engine identifies which component receives `audio: [user]` and which sends `audio: [user]`. These are the bridge's audio input and output endpoints. Same for text, image, video.

4. **Wires tools**: The component with `harness: core` is given full Hermes agent access. Other components get only their declared tools. Tool invocations are routed to the target component.

5. **No FSM**: The graph replaces the session state machine. Data flows through push routes. Components process independently. The only state is per-component (busy/idle) for interrupt gating.

## Example Profiles

### JARVIS — Unified realtime

```yaml
jarvis:
  components:
    brain:
      ins:
        text: [user]
        audio: [user]
        image: [user]
        video: [user]
      outs:
        text: [user]
        audio: [user]
      tools:
        harness: core
      io:
        video: {mode: live, interrupt: false}
        audio: {mode: live, interrupt: true}
```

Single component, all I/O, full harness. The simplest profile.

### Thinker-Talker

```yaml
thinker-talker:
  components:
    talker:
      ins:
        audio: [user]
        text: [thinker]
      outs:
        text: [thinker]
        audio: [user]
      tools:
        defer: thinker
    thinker:
      ins:
        text: [user, talker]
        image: [user]
        video: [user]
      outs:
        text: [user, talker]
      tools:
        harness: core
        image: capture
        video: tape
      io:
        video: {mode: tape}
        image: {mode: frame}
```

Talker handles audio I/O. Thinker handles reasoning, tools, vision. Text from the user goes directly to the thinker. Audio goes through the talker first.

### Fast Talker

```yaml
fast-talker:
  components:
    talker:
      ins:
        audio: [user]
      outs:
        audio: [user]
      tools:
        defer: core
    core:
      ins:
        text: [user]
      outs:
        text: [talker, user]
      tools:
        harness: core
```

Small TTS-optimized model handles voice. Anything non-trivial gets deferred to the core via tool call.

### GLaDOS — Full frankenstein

```yaml
glados:
  components:
    ears:
      ins: {audio: [user]}
      outs: {text: [talker]}
      io: {audio: {mode: live, interrupt: false}}
    talker:
      ins: {text: [ears, thinker]}
      outs: {text: [thinker]}
      tools: {defer: thinker}
    thinker:
      ins: {text: [talker, user], image: [user], video: [user]}
      outs: {text: [talker, user]}
      tools: {harness: core, video: tape}
      io: {video: {mode: tape}, image: {mode: frame}}
    mouth:
      ins: {text: [talker, thinker]}
      outs: {audio: [user]}
```

Four components, no single component has full I/O. Audio flows: user → ears → talker → thinker → mouth → user. Text can go user → thinker directly.