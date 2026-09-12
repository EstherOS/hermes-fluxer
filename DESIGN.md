# fluxer — design

Status: **v1 built + sandbox-verified (2026-09-11).** All phases implemented; voice is
half-duplex v1 (barge-in deferred); the omni engine exposes stitched + unified seams
with local GGUF backends filling the senses. See `STATUS.md` for the ledger and
`docs/` for reports.

**Separation update (2026-09-11):** fluxer (the chat platform adapter) and the
realtime/omni engine are now specified as independent projects. See:
- `docs/spec-fluxer-plugin.md` — fluxer = Hermes platform plugin for fluxer.app
  messaging. Text + attachments only. Voice/video features optional via bridge to
  the realtime engine.
- `docs/spec-roles-profiles.md` — realtime/omni engine = role-tagged multimodal
  backends, profile composition, session FSM, cancellation protocol. Platform-agnostic;
  usable by fluxer, discord, or any Hermes platform.

The two projects share the same workspace but are independent packages with no
required dependency between them.

## Goal
Fluxer as a first-class Hermes surface (gateway platform plugin), omnimodal:
text, image, audio, video, in and out. A single omni model OR a stitched
multi-model stack are both just config of the same interfaces. Strong seams
between modes; no forced separation (voice transcripts, attachments, text can
share one session where wanted).

## Shape
- Platform plugin: ~/.hermes/hermes-agent/plugins/platforms/fluxer/ — same seam
  as the discord/irc plugins (plugin.yaml + adapter.py + register(ctx)); runs
  inside the gateway process. No parallel services.
- Engine: plugins/platforms/fluxer/omni/ — modality channels + backend slots.
  Transport-agnostic; reusable by other platforms later.
- Docs/scripts/research: /home/agent/workspace/fluxer/.

## Engine seams (v0, realtime engine spec in docs/spec-roles-profiles.md)
Four senses, two directions each: text, image, audio, video (in/out).
- Part: one modality payload (bytes/str + mime + meta).
- Backend slots (one role each): ears, mouth, eyes, talker, thinker, realtime, omni.
- Profiles bind senses to backends; mode = stitched | unified.
  - stitched: per-sense bindings with fallback chains.
  - unified: one duplex backend covers N senses — identical downstream interface,
    so nothing else changes when such a model exists.
- Voice session binding (config): ephemeral | channel | invoke.
- Voice transcripts (config): off | on (target configurable).
- Video: VideoDuplexer interface (push frames in / pull frames out); backend
  list starts with a local pipeline (baby models on the 1650), realtime model
  later.

## Phases (one mode at a time, per Kairo)
1. Text — connect, DM + #general, allowlist, fail-closed policy, sessions, replies.
2. Commands — /new first (+ a few basics); slash commands, text fallback.
3. Attachments — receive (download -> agent context) and send (uploads).
4. Voice — join/leave; cascade STT -> agent -> TTS first; session-binding +
   transcript config; realtime duplex behind the same seam.
5. Video — duplexer framework + local baby-model backend; graceful fallback
   when GPU path is limited.

## Testing
- Unit: checkout tests/plugins/platforms/test_fluxer_*.py (mirror discord tests).
- Live: scripts/ against the real bot (guild test + Kairo DM); read-back verification.
- Integration: plugin load in a sandbox gateway instance (separate HERMES_HOME);
  the live gateway (PID 1) is never restarted unplanned.

## Safety (night policy)
- Live gateway = PID 1: no restarts.
- All local model/test runs nice'd, thread-capped, short; load watchdog alerts.
- Writes to Fluxer only via controlled test scripts (one-shot, cleaned up).

## Open questions (research wave)
- Attachment upload mechanics (multipart vs presigned).
- Slash command registration/response flow.
- Voice relay protocol specifics (transport, opus, receive support).
- Config placement: platform extra vs top-level `omni:` section.