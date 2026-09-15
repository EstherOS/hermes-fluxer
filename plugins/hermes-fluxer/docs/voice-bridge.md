# Voice bridge

The voice bridge connects a LiveKit voice call to the omni graph. It owns call-side audio I/O: capture, utterance segmentation, graph feeding, and playback.

## Lifecycle

```
room.join()  →  bridge starts, attaches its output handler to the binding,
                opens the omni session
room.leave() →  bridge detaches, closes the omni session
```

## Mic → graph

1. LiveKit delivers 48 kHz PCM frames from the mic track.
2. The VAD segments the audio into utterances. When sustained speech is detected (~300 ms), the bridge feeds an onset envelope at `@fluxer.speech`; profiles route it at `<node>.cancel` endpoints for barge-in.
3. A completed utterance is written to a WAV file and fed into the graph at `@fluxer.audio`.
4. ASR nodes transcribe it, and routes carry the transcript onward.

## Graph → speaker

1. Audio envelopes routed to `@fluxer.audio_out` are received by the binding and handed to the bridge.
2. The bridge plays them through a serialized queue — one segment at a time — resampled for the LiveKit speaker track.
3. When an execution is cancelled (control endpoint, new turn, or call end), the bridge flushes its queue and aborts the in-flight segment so the speaker goes quiet immediately.
