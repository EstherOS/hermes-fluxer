# Envelope

The universal message unit flowing through the graph.

```python
@dataclass
class Envelope:
    type: str            # audio, video, text, transcript, tool_call, tool_result, event, control, error
    payload: Any         # modality-specific (bytes, str, dict)
    session_id: str
    turn_id: str
    execution_id: str    # correlation handle for cancellation
    source: str | None   # endpoint that produced this envelope
    parent_execution_id: str | None
    timestamp_ms: int
    metadata: dict
```

Every routed thing is an envelope. The engine matches routes against `envelope.type` for `when:` selectors.

## Standard types

audio, video, text, transcript, tool_call, tool_result, event, control, error

The engine does not validate payload structure — that is the adapter's responsibility.

`control` envelopes are adapter-interpreted signals: the payload is the adapter's contract. Engine-executed operations are not envelopes — they are addressed to reserved control endpoints (`<node>.cancel`, see the schema) and never handed to an adapter.