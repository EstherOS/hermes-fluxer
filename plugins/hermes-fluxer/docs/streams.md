# Platform streams

The fluxer binding exposes the `@fluxer.*` endpoints that profiles wire against.

## `@fluxer.audio`

Mic input. The voice bridge feeds each detected utterance into the graph here as an `audio` envelope. ASR nodes consume it (`local/crispasr`).

## `@fluxer.audio_out`

Speaker output. Audio envelopes routed here are handed to the bridge's playback queue and pushed to the LiveKit speaker track, resampled for the platform.

## `@fluxer.speech`

Speech-onset signal. The bridge feeds one envelope here each time the VAD detects sustained speech. It carries no payload — route it at control endpoints (`<node>.cancel`) to stop in-flight work.
