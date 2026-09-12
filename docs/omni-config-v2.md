# Omni Engine Config v2 — Component Grammar

## Concepts

A profile is a **graph of components**. Each component is a model or service that takes inputs, produces outputs, and optionally exposes tools to other components.

The graph has two interaction mechanisms:

- **Push (ins/outs)**: Data flows automatically from source to destination(s). When a component produces output, every component in its `outs` list receives it immediately.
- **Pull (tools)**: A component decides *when* to call another component. Tools are named capabilities exposed by one component for others to invoke on demand.

Every data connection has a **mode** (live / tape / frame) and an **interrupt policy** (true = barge-in cancels current work; false = queue until ready).

## Grammar

```yaml
omni:
  profiles:
    <name>:
      components:
        <name>:
          ins:                          # what this component listens to
            <sense>: [<source>, ...]    # source is "user" or another component name
          outs:                         # what this component pushes to
            <sense>: [<dest>, ...]      # destination is "user" or another component name
          tools:                        # tools this component exposes to others
            <tool_name>: <target>       # target is another component name or "core" (full harness)
          # Per-connection properties (optional, override defaults)
          io:
            <sense>:
              from: <source>
              mode: live | tape | frame    # default: live for audio, tape for video, frame for image
              interrupt: true | false      # default: true for audio, false for rest

        # ── or single-model shorthand ──
        brain: {model: <backend>, senses: [<sense>, ...]}

      routes:   # optional edge overrides
        <from_component>.<sense>: [<to_component>, ...]

      # ── or legacy shorthand ──
      mode: unified | stitched
      backend: <backend>
      bindings: ...
```

## Semantics

### ins

Declares what this component receives. Sources can be:

- `user` — direct from the voice call (mic audio, camera video, text channel, screenshare)
- `<component_name>` — output from another component in the profile

A component's `ins` defines the data it's capable of processing. If no component claims a sense in its `ins`, that sense is unavailable (user input silently dropped).

### outs

Declares what this component pushes to. Destinations can be:

- `user` — to the voice/text channel
- `<component_name>` — to another component

When a component produces output of a sense type, it pushes to ALL destinations listed in `outs` for that sense simultaneously. Fan-out is the default.

### tools

Tools are named interaction points a component exposes for other components to call. Unlike `outs` (push), tools are called *on demand* by the consumer.

- `tools: {defer: thinker}` — this component has a "defer to thinker" tool that calls the thinker component
- `tools: {harness: core}` — this component has full access to the Hermes harness (tools, context, system prompts)
- `tools: {image: eyes}` — this component can call the eyes component for image analysis

The `core` target is special — it grants the component full Hermes agent capabilities. Only one component should typically have `harness: core`.

### io modes

Per-connection properties:

| property | values | default | description |
|---|---|---|---|
| `mode` | `live`, `tape`, `frame` | `live` for audio, `tape` for video, `frame` for image | How data is captured: live stream, pre-recorded clip, or single frame |
| `interrupt` | `true`, `false` | `true` for audio, `false` for rest | Whether new input cancels current processing |

### Tempo emergence

A component's tempo is **not declared**. It is inferred from its io modes:

- All connections `live` → `realtime` (continuous streaming)
- Mixed `live` + `tape`/`frame` → `fast_half_duplex` (some senses are live, others are on-demand)
- All connections `tape` or `frame` → `turn` (everything is file-based)

The engine reads the graph and determines the tempo automatically.

### Mode emergence

A profile's mode is also inferred:

- Single component with all senses → `unified`
- Multiple components, linear chain → `stitched` / `cascade`
- Bidirectional link between two components → `talker-thinker`
- Component with no user-facing `outs` (only routes to others) → `router`
- Four+ components with mixed tempos → `glados` / `full frankenstein`

The mode label is documentation. The engine treats all profiles the same — it builds the component graph and wires the push routes.

## Example Profiles

### JARVIS — Unified realtime

One model handles everything. Full duplex, all senses, tool access.

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
        video:
          mode: live
          interrupt: false
        audio:
          mode: live
          interrupt: true
```

Engine infers: mode=unified, tempo=realtime, core=brain.

### Thinker-Talker — Realtime frontend + deep core

A fast realtime model handles voice I/O. A deep turn-based model handles reasoning and tools. They talk bidirectionally. The thinker also gets video (taped) and direct text input.

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
      io:
        audio:
          mode: live
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
        video:
          mode: tape
        image:
          mode: frame
```

### Fast Talker — Lightweight frontend delegates to subagents

A small, fast model optimized for TTS handles voice interaction. It has no deep reasoning capability — it delegates everything to smarter subagents. This is the "pseudo-realtime" approach.

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
        image: core
        search: core
      io:
        audio:
          mode: live
    core:
      ins:
        text: [user]
      outs:
        text: [talker, user]
      tools:
        harness: core
```

The talker handles audio I/O. When it needs to do anything non-trivial, it calls `defer: core` which sends the context to the core. The core processes, responds, and pushes the response text back to the talker (for TTS) and to the user (for text display).

### GLaDOS — Full frankenstein

A dedicated ASR model → fast talker → deep thinker → TTS. Four components, mixed tempos, no component has full I/O. Everything is harness trickery.

```yaml
glados:
  components:
    ears:
      ins:
        audio: [user]
      outs:
        text: [talker]
      io:
        audio:
          mode: live
          interrupt: false
    talker:
      ins:
        text: [ears, thinker]
      outs:
        text: [thinker]
      tools:
        defer: thinker
    thinker:
      ins:
        text: [talker, user]
        image: [user]
        video: [user]
      outs:
        text: [talker, user]
      tools:
        harness: core
        video: tape
      io:
        video:
          mode: tape
        image:
          mode: frame
    mouth:
      ins:
        text: [talker, thinker]
      outs:
        audio: [user]
      io:
        audio:
          mode: live
```

Ears (stream ASR) → text → talker (fast half-duplex, decides) → thinker (deep turn, harness) → talker or mouth for TTS.