# fluxer — design

Fluxer (fluxer.app) as a first-class Hermes surface: chat plus an omnimodal
realtime engine, with clean seams — a single unified model or a stitched local
stack are both just config.

## Packages
- `packages/omnimaker/` — the graph engine: routes envelopes between nodes;
  adapters do the work. Transport-agnostic; no platform dependencies.
- `packages/omnimaker-hermes/` — the `hermes/session` adapter: wraps a Hermes
  agent conversation as an engine node (system prompt, tools, memory).
- `plugins/hermes-fluxer/` — the Fluxer platform plugin: REST + gateway
  clients, adapter, media, and the LiveKit voice lane (`voice/`) with the
  omni/video lanes (`omni/`, `video/`). Loads inside the gateway process;
  degrades gracefully — text-only without the omni engine, voice disabled
  without LiveKit.

## Engine seams
Senses, two directions each: text, image, audio, video (in/out).
- A **part** is one modality payload (bytes/str + mime + meta).
- **Backends** fill the slots (one role each): ears, mouth, eyes, talker,
  thinker, realtime, omni.
- **Profiles** bind senses to backends — *stitched* (per-sense bindings with
  fallback chains) or *unified* (one duplex backend covering N senses; the
  downstream interface is identical, so nothing else changes when such a model
  exists).
- Voice session binding: `ephemeral | channel | invoke`; voice transcripts:
  `off | channel`.
- Video: a duplexer interface (frames in / events out) with a local GGUF
  pipeline backend.

## Testing
- Unit: `plugins/hermes-fluxer/src/fluxer/tests` (pytest).
- Live: `scripts/` against the real bot (guild test + DM); read-back verification.
- Integration: plugin load in a sandbox gateway instance (separate HERMES_HOME);
  the live gateway is never restarted unplanned.

## Safety
- Live gateway = PID 1: no restarts.
- Local model/test runs nice'd, thread-capped, short; load watchdog alerts.
- Writes to Fluxer happen via controlled test scripts (one-shot, cleaned up).
