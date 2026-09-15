# Routing primitives

The engine ships built-in routing nodes, registered as `omni/*`. They are ordinary nodes from the engine's perspective — they appear in `nodes:` and connect via `routes:` like any other participant.

## `omni/tee`

Fan-out: copy each incoming envelope to the named outputs `a`, `b`, `c` — one route per output port.

```yaml
nodes:
  tee:
    use: omni/tee
routes:
  - from: ears.transcript
    to: tee.input
  - from: tee.a
    to: talker.input
  - from: tee.b
    to: logger.input
```

## `omni/merge`

Fan-in: combine the `a` and `b` inputs into one `output` stream.

## `omni/filter`

Pass through only envelopes matching a predicate.

```yaml
nodes:
  finals:
    use: omni/filter
    config:
      predicate: "type == transcript"
```

## `omni/buffer`

Accumulate envelopes and emit the batch once it reaches `max` (default 16).

```yaml
config:
  max: 16
```

## `omni/debounce`

Pass through an envelope, then drop arrivals for `window_ms` (default 300).

```yaml
config:
  window_ms: 300
```

## `omni/throttle`

Limit throughput to `rate` envelopes per second (default 10).

```yaml
config:
  rate: 10
```

These primitives keep the engine core small while supporting common composition patterns. Anything workflow-shaped beyond this set (retry, batching across sessions, custom sequencing) is pushed into a reusable node rather than growing the YAML schema.
