# C4b report — fluxer voice module (wave 3, full)

**Date:** 2026-09-11 ~08:48–09:05 UTC · **Author:** C4b · **Scope:** `plugin-src/fluxer/voice/` + adapter
integration + unit tests + bounded live evidence.
**Verdict: ✅ the wave-3 voice module works end-to-end as far as a headless bot can prove it** —
join → LiveKit → module-speak over the wire → STT ear → cascade dry-run → clean leave → missing-deps
degradation. The only unproven legs remain the ones that need a second human/account in the channel
(real receive), the 600 s grant long-run, and DM calls (same list as c4a §5).

---

## 1. Deliverables (all under `/home/agent/workspace/fluxer/`)

| Path | What | Lines |
|---|---|---|
| `plugin-src/fluxer/voice/__init__.py` | lazy exports; `try_livekit()` log-once gate (`voice disabled: install livekit …`) | 83 |
| `plugin-src/fluxer/voice/config.py` | `VoiceConfig` + total `parse_voice_config()` (`extra.voice.*`); bad values → defaults + warning | 241 |
| `plugin-src/fluxer/voice/audio.py` | resample (audioop, linear fallback), wav I/O, frame helpers, energy VAD segmenter (`silence_ms`/`min_utterance_ms`/`max_utterance_s`/pre-roll) | 281 |
| `plugin-src/fluxer/voice/cascade.py` | `track_subscribed` → `rtc.AudioStream` → VAD → utterance WAV → STT (hermes / whispercpp) → adapter pipeline; single-utterance gate; fail-closed authz gate; transcripts echo | 353 |
| `plugin-src/fluxer/voice/controller.py` | `VoiceController` + `VoiceSession`: op4 join, VSU wait, room connect, bindings, piper→48k→`AudioSource` publish, bounded re-op4 rejoin, `token_refreshed`, auto-leave, `/voice` info helpers | 898 |
| `plugin-src/fluxer/adapter.py` | integration (voice section + send hook + event routing + core methods) — details §6 | +~210 |
| `plugin-src/fluxer/tests/test_voice.py` | 33 unit tests, fake `livekit.rtc` stub, no network | 941 |
| `scripts/voice_c4b_live.py` | bounded live-evidence runner, phases a–d, redacted evidence | 465 |
| `status/c4b-evidence/voice-c4b-20260911-090333.jsonl` + `.summary.json` + `canonical-run.console.log` | **canonical run** (32 records, all phases ok) | — |
| `status/c4b-evidence/leftover-check.txt` | post-run fresh-gateway check (09:04:36Z): `voice_states = []` (no stale presence) | — |
| `status/c4b-evidence/voice-channel-text-probe.json` | type-2 voice channel accepts + deletes text posts (transcripts=channel viable) | — |

Rerun evidence: `cd /home/agent/workspace/fluxer && nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/voice_c4b_live.py --phase all`
Tests: `cd /home/agent/.hermes/hermes-agent && ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests -q` → **240 passed** at final handoff (126 wave-1/2 baseline + 33 voice + sibling video/omni lanes landing concurrently — the total grows as they add tests; the voice lane's own 33 are stable and green).

## 2. Config (`gateway.platforms.fluxer.extra.voice.*`)

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | false → everything inert (no livekit probe, no events, join refuses) |
| `channels` | `[]` | **hard allow-list when non-empty** (all joins refused otherwise); empty = explicit joins unrestricted, `auto_channels` still honored |
| `auto_channels` | `[]` | `"guild:channel"` / `"guild/channel"` / `{guild_id, channel_id}` / bare channel id (+`guild_id`) — joined at gateway connect |
| `guild_id` | `null` | default guild for bare channel ids in `auto_channels` |
| `session_binding` | `channel` | `ephemeral` \| `channel` \| `invoke` (see §5) |
| `transcripts` | `off` | `off` \| `channel` (`on` accepted as alias); `channel` posts STT text + reply text into the bound channel |
| `stt.engine` | `hermes` | `hermes` = `tools.transcription_tools.transcribe_audio` (faster-whisper, model cached process-wide) — used by the discord voice lane too; `whispercpp` = vendored CLI |
| `stt.model` | `null` | faster-whisper size passed to the hermes path (e.g. `base`, `tiny`) |
| `stt.threads` | `2` | whisper.cpp threads |
| `stt.language` | `en` | |
| `stt.binary` / `stt.model_path` | `gpu/tools/whisper-bin-ubuntu-x64/whisper-cli` / `models/ggml-tiny.en.bin` | whispercpp engine |
| `tts.engine` | `piper` | only engine in v1 |
| `tts.binary` / `tts.model` | `gpu/tools/piper/piper` / `models/en_US-lessac-medium.onnx` | `tts.voice` reserved |
| `silence_ms` | `900` | VAD silence debounce (utterance end) |
| `min_utterance_ms` | `300` | shorter bursts dropped |
| `max_utterance_s` | `30` | force-emit |
| `energy_threshold` | `400` | int16 RMS |
| `auto_leave_after_s` | `0` (off) | leave after N s with an empty room |
| `rejoin_max_attempts` | `3` | bounded re-op4 rejoin, backoff 2·n s ≤ 15 s (0 = never rejoin) |
| `join_timeout_s` | `20` | VSU wait + room connect budget |
| `speak_timeout_s` | `90` | per-utterance TTS+publish cap |
| `speak_dedupe_s` | `30` | duplicate-speech guard window |

## 3. Live evidence (canonical run `voice-c4b-20260911-090333`, nice 19, ~50 s wall)

**(a) join → speak → leave (real gateway + real LiveKit, guild 1547815091221561344 / channel …348)**
- gateway READY 1.98 s; **op4 join → room connected in 3.05 s**, `connection_id: raven-algol`,
  room `guild_1547815091221561344_channel_1547815091221561348` (`RM_L2wrgcEk4QR6`), binding `channel:1547815091221561348`.
- **speak: piper → 48 kHz mono PCM → published track `TR_AMreQ2FjvRB94M`, 254 frames**;
  wire proof via `track.get_stats()`: `candidate_pair PAIR_SUCCEEDED nominated:true packets_sent:236 bytes_sent:59672`
  (earlier run), `outbound_rtp kind audio packets_sent:222`, `media_source total_audio_energy ≈1.5,
  total_samples_duration ≈4.9 s`, `DTLS_TRANSPORT_CONNECTED / ICE_TRANSPORT_CONNECTED`. `wait_for_playout()` drained.
- `token_refreshed` fired once, SDK-adopted (controller counts it; value never logged).
- clean leave: op4 `channel_id=null` + room disconnect; **fresh-gateway check 09:04:36Z → `voice_states = []`**.

**(b) STT ears offline** (piper sentence → both backends, word-overlap vs the spoken text):
- whisper.cpp (vendored `ggml-tiny.en`, 2 threads): “The Wave 3 Voice module is online and hearing every word.”, overlap **0.9**, 8.6 s CPU.
- hermes path (`transcribe_audio`, faster-whisper `tiny`, CPU int8, scratch HERMES_HOME — includes the one-time model download in a bounded subprocess): same text, overlap **0.9**, 13.5 s.

**(c) cascade dry-run (fake transport, real frames/STT/piper)**
- 234 received frames (2.4 s speech + trailing silence) → **1 utterance → 1 STT call → transcript
  “The Wave 3 Voice module is online and hearing every word.” → dispatched** as a synthetic `MessageEvent`
  (chat_id = voice channel, `chat_type=channel`, user_id 1473728643747861346, `MessageType.TEXT`) via `handle_message`.
- mock reply → `piper` → **195 PCM frames / 374 400 bytes (3.9 s) captured at the AudioSource**, `publish_track` called once (`TR_dryrun`).

**(d) missing-deps simulation** (`sys.modules["livekit"] = None` in a subprocess)
- plugin + adapter import fine; `join_voice_channel` → `False`; **one** log line
  `Fluxer voice disabled: install livekit into the Hermes venv (…) — text-only mode`.

## 4. Unit coverage (`tests/test_voice.py`, fake `livekit.rtc` stub, no network)

- session lifecycle (join/leave, op4 exactly once, idempotent re-join, leave cleanup);
- all three binding modes + `invoke` fallback (warning + channel binding);
- `channels` allow-list refusal; join timeout (no VSU) → graceful fail + cleanup op4;
- VAD: speech+silence segmentation, short-utterance drop, max-length force-emit, flush, resample/wav round-trip;
- cascade: frames→STT→`handle_message` (asserts text/chat_id/chat_type/user_id), reply→`send`→publish frames, concurrent-utterance drop, unauthorized-speaker drop **before STT** (fail-closed), core-callback preference, `track_subscribed` reader path via a fake `AudioStream`;
- send hook matrix: transcripts off (speak-only) / channel (post+speak) / piper failure (reply always posted) / auto-TTS active (no double-talk, no post) / `play_tts` routes into the VC;
- core-probe surface (`is_in_voice_channel`, `get_voice_channel_info`, `get_voice_channel_context`, `get_user_voice_channel`, `join/leave_voice_channel`);
- lazy-import failure (plugin text-only, hint once) + `enabled=false` inertness.

## 5. Session binding semantics (implemented)

| Mode | binding chat | notes |
|---|---|---|
| `channel` (default) | `chat_id = voice channel id`, `chat_type=channel`, scope guild | its text lives in the same voice channel (verified writable, §1 probe) |
| `invoke` | captured source at join (`/voice join` message source; programmatic joins may pass `source=`) | fallback → channel + warning when no source (documented; core `_handle_voice_channel_join` calls `join_voice_channel(channel)` without a source, so the adapter stashes the `/voice` command source) |
| `ephemeral` | `voice:{channel_id}:{connection_id}` (connection id known after VSU; updated at room connect), `chat_type=voice`, isolated, no transcript echo | |

Replies route back through `adapter.send()` for the bound chat: **speak via piper**, gated by
`transcripts` (off = suppress the text post, channel = post + speak). `TTS failure never eats a
reply`: with transcripts=channel the post goes first; with transcripts=off a speak failure falls
back to a text post (logged). When the core auto-TTS lane is active for the chat (`/voice on` sets
`_auto_tts_enabled_chats`), the piper hook stands down and `play_tts` (Hermes TTS engine → our
`play_in_voice_channel`) owns speech — exactly one spoken copy per turn.

## 6. Adapter integration map (surgical edits, all in `adapter.py`)

- imports: `.voice.config` + `.voice.controller` (no livekit at import time); `_VOICE_EVENT_TYPES`.
- `__init__`: `_voice_cfg` (parsed once), `_voice`, `_voice_start_task`, `_voice_text_channels`,
  `_voice_sources`, `_voice_input_callback`, `_last_command_source` (discord-parity names so
  `gateway/slash_commands.py` + `gateway/run_voice.py` hasattr-probes hit).
- `connect()`: `_voice_start_after_connect()` — builds the controller (lazy, gated by
  `try_livekit()`), seeds voice states from READY, kicks off `auto_channels`.
- `_teardown_transport()`: `voice.shutdown()` (op4 null + room disconnect) **before** the WS dies.
- `_on_gateway_event()`: `VOICE_SERVER_UPDATE` / `VOICE_STATE_UPDATE` / `GUILD_CREATE` →
  `_route_voice_event()` (errors isolated); `_on_connection_event("ready")` seeds voice state.
- `_handle_message_create()`: exposes `raw_message=SimpleNamespace(guild_id=…)` on guild events
  (makes core `_get_guild_id` work for `/voice`) and stashes `/voice` command sources for `invoke`.
- `send()` → dispatcher; original body moved verbatim to `_send_text_chunked()`; voice-bound chats go
  through `_voice_bound_send()` (§5 gating).
- new methods (names/signatures match the core probes): `play_tts`, `join_voice_channel`,
  `leave_voice_channel`, `is_in_voice_channel`, `get_voice_channel_info`,
  `get_voice_channel_context`, `get_user_voice_channel`, `play_in_voice_channel`.

## 7. Proven vs needs-kairo vs blocked

**Proven live:** join/VSU/room-connect; piper→PCM→publish with wire stats; clean leave (no stale
presence); `token_refreshed` adoption observed; STT with both backends on real speech; full cascade
dry-run (frames→text→dispatch→reply→PCM); graceful no-livekit plugin load; voice channels accept text.

**Proven unit-only:** rejoin/backoff/exhaustion, binding modes, VAD edges, transcripts matrix,
config parsing, core-probe surface (join/leave/status plumbing).

**Needs kairo / a human (explicitly not provable headless):**
1. **Receive-from-real-human leg** — a second speaker publishing a track → `track_subscribed` →
   `AudioStream` on the wire → transcript. (Reader path works with synthetic frames; the c4a invite stands.)
2. **600 s grant long-run** — need a ≥10 min session to see mid-session refresh/expiry behavior;
   the controller is built for re-op4-rewait-reconnect and adopts `token_refreshed` meanwhile.
3. **DM calls** (`dm_channel_{id}`, `guild_id: null`) — guild flow only.
4. **Core `/voice join` from chat** — needs the human sitting in a voice channel
   (`get_user_voice_channel` reads routed voice-state cache) and a text channel trigger; then
   `/voice join|leave|status` should work with no core edits (methods + state mirrors are in place).
5. Barge-in / duplex turn-taking — deferred (spec §6-W3 duplex seam); v1 is half-duplex, single
   utterance at a time, concurrent speech dropped with a log.

## 8. Deviations / decisions (documented where the task left them open)

1. `transcripts: on` (spec §6 wording) accepted as an alias of `channel`.
2. `channels` empty = explicit joins unrestricted (auto_channels still honored); non-empty = hard allow-list.
3. Cascade prefers the core `_voice_input_callback` when `/voice join` wired it (discord parity:
   authz + duplicate suppression + echo live in the runner); otherwise it builds its own
   `MessageEvent` from the binding (MessageType.TEXT, `raw_message` guild hint).
4. speak-dedupe (30 s, ≥0.95 ratio) as a belt-and-braces double-talk guard beside the auto-TTS gate.
5. One voice session per guild: joining another channel in the same guild moves (old room torn down).
6. Ephemeral binding id carries the *connection id* (only known after VSU) — binding re-keyed at room connect.
7. Extra knobs beyond the spec: `energy_threshold`, `speak_timeout_s`, `speak_dedupe_s`, `tts.engine`.

## 9. Unknowns

1. Grant expiry semantics at 600 s (c4a §5.1) — strategy re-op4 same channel; untested long-run.
2. `token_refreshed` payload/effect beyond "SDK adopts it" (never logged — it is a credential).
3. LiveKit behavior drift beyond 1.1.18 (`Room.sid` async, proto/python enum mix — handled).
4. Whether Fluxer ever pushes host-only endpoints (probe prepends `wss://` as before).
5. Voice-input authorization: before STT, the adapter's local allowlist mirror
   (`FLUXER_ALLOWED_USERS` / `FLUXER_ALLOW_ALL_USERS`, fail-closed) is applied by the cascade module;
   the core-callback path re-checks in the runner — but a production guild needs one of those env
   vars set for voice to be heard at all (same posture as text).
6. `auto_leave_after_s` and `rejoin` were exercised in unit tests, not against the live server.
