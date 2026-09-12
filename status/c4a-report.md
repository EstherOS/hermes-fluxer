# C4a report — voice feasibility probe (wave 2)

**Date:** 2026-09-11 ~08:30–08:33 UTC · **Author:** C4a · **Verdict: ✅ voice path PROVEN as far as a headless bot can**
(op4 join → VOICE_SERVER_UPDATE → LiveKit connect → publish piper speech over the wire → leave).
The only unproven leg is **receive** (needs a second speaker — cannot be done headless tonight).

---

## 1. Ingredients (exact)

| Item | Value |
|---|---|
| Python | `/home/agent/.hermes/hermes-agent/venv/bin/python` — 3.11.16 |
| Install cmd | `/home/agent/.hermes/bin/uv pip install --python /home/agent/.hermes/hermes-agent/venv/bin/python livekit` |
| Installed | **livekit==1.1.18** + aiofiles==25.1.0 + types-protobuf==7.35.1.20260906 (dry-run said 1.1.16; resolver picked 1.1.18 — fine, small, pure install into the venv only) |
| Import path | `from livekit import rtc` — native FFI as `livekit/rtc/resources/liblivekit_ffi.so`, loads fine in this container (logs `Nvidia Decoder is supported.`) |
| API used (read from the installed package before coding, not guessed) | `rtc.Room()` / `await room.connect(url, token)` / `await room.disconnect()`; `rtc.RoomOptions(auto_subscribe=True)` default; events `room.on("connection_state_changed"\|"disconnected"\|"local_track_published"\|"track_subscribed"\|"active_speakers_changed"\|"connection_quality_changed"\|"token_refreshed"\|…)`; `rtc.AudioSource(48000, 1)`; `rtc.LocalAudioTrack.create_audio_track(name, source)`; `rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)` (enum value 2); `rtc.AudioFrame.create(rate, ch, samples_per_channel)` + `.data` int16 memoryview; `await source.capture_frame(frame)`; `await room.local_participant.publish_track(track, opts)`; `await source.wait_for_playout()`; `await track.get_stats()` |
| 1.1.x quirks found | `room.sid` is **async** (`await room.sid` — otherwise you log a coroutine); `rtc.ConnectionQuality` is a plain Python enum (`.name`), `rtc.DisconnectReason`/`ConnectionState`/`TrackSource` are protobuf wrappers (`.Name(int)`) |
| Piper | `echo "Hello from the night shift. The Fluxer voice path is alive." \| gpu/tools/piper/piper --model models/en_US-lessac-medium.onnx --output_file /tmp/vp.wav` → 3.62 s speech, 22050 Hz mono Int16 (resampled to 48 kHz mono in-probe) |
| Channel | guild `1547815091221561344`, voice channel `1547815091221561348` (type 2 "General" under "Voice Channels"; bitrate 64000, voice_connection_limit 5, overwrites none) — **already existed, no creation needed** |

## 2. Verified flow (canonical run 2, 08:31:31–08:31:49Z ≈ 18 s wall, nice 19)

1. **REST check** — `GET /guilds/{gid}/channels` → voice channel found, type 2. (read-only)
2. **Gateway** — C1 client reused via sys.path (`sys.path += [checkout, plugin-src]`); READY in **2.23 s**; user `1547828742208888832` (Esther); `rtc_regions` 14; pre-join `GUILD_CREATE.voice_states` = `[]`.
3. **op4 join** — `update_voice_state(guild, channel, self_mute=False, self_deaf=False)` → `VOICE_SERVER_UPDATE` **0.037 s** later:
   - `connection_id: "bobtail-banded"` (run 1: `"cow-great"` — new per join), `endpoint: "wss://d29163f.arn.fluxer.media"` (run 1: `wss://318166d.fra.fluxer.media`; region varies per join; both runs got a full `wss://` URL — the host-only form wasn't hit live, the probe prepends `wss://` anyway).
   - **token REDACTED in all evidence** — kept as `{redacted, len: 895, prefix: "eyJhbGci"}` (run 1: len 881). Raw token never logged; verified by scanning evidence for long `eyJ*` runs → none.
4. **Grant claims** (decoded JWT *payload* only; signature never touched): `roomJoin: true`, `canPublish: true`, `canPublishSources: ["microphone","camera","screen_share","screen_share_audio"]`, `canSubscribe: true`; `sub: user_1547828742208888832_bobtail-banded`; room `guild_1547815091221561344_channel_1547815091221561348`; `exp − issued_at = 600.0 s` — **doc §6's 600 s grant lifetime confirmed exactly**. (metadata: region `eu-central`, server `d29163f-arn`.)
5. **Own VOICE_STATE_UPDATE** — `suppress: false`, `mute: false`, `deaf: false`, `self_mute/self_deaf: false`, same `connection_id` → **joined, and SPEAK is NOT suppressed** (consistent with the grant).
6. **LiveKit connect** — `rtc.Room().connect("wss://…", token)` OK in **3.22 s**; room name exactly `guild_1547815091221561344_channel_1547815091221561348` (matches docs); room `sid: RM_wd8p7c52XAZ8`; state `CONN_CONNECTED`; local identity `user_1547828742208888832_bobtail-banded`; remote participants `[]` (empty channel — no one to hear it, by design).
7. **Publish** — `TR_AMyC9HSf4QNEY2`, name `hermes-voice-probe`, source 2 (microphone), in **0.121 s**; `local_track_published` fired. Pushed **181 frames / 3.616 s** of real speech PCM (48 kHz mono) in 2.61 s wall, then 5.0 s of silence hold; `wait_for_playout()` drained OK; unpublish clean.
8. **Wire proof** (`track.get_stats()`, the money shot): `candidate_pair: PAIR_SUCCEEDED, nominated: true, packets_sent: 200, bytes_sent: 44638, packets_received: 28`; `outbound_rtp: kind audio, ssrc 3595338823, packets_sent: 185, bytes_sent: 37292`; transport `DTLS_TRANSPORT_CONNECTED / ICE_TRANSPORT_CONNECTED`; `media_source: total_audio_energy: 1.007, total_samples_duration: 8.56 s` (= 3.62 speech + 5.0 silence). Codec negotiated `audio/opus` pt 111, 48 kHz. Quality event `QUALITY_EXCELLENT`.
9. **`token_refreshed`** room event fired once per run right after connect — the server pushes a replacement grant and the SDK adopts it silently (value never logged). Not yet investigated what triggers it or whether it extends the session; flagged in §5.
10. **Leave / no leftovers** — op4 `channel_id=null` sent, room disconnected (`CLIENT_INITIATED`), ws closed. Independent check 08:32Z: fresh gateway connect → `GUILD_CREATE.voice_states = []` → **no stale voice presence**.
11. Non-fatal noise: one FFI log line `publisher data channel '_data_track' closed unexpectedly` during publish negotiation (both runs); audio path unaffected.

## 3. Files (all under `/home/agent/workspace/fluxer/`)

| Path | What |
|---|---|
| `scripts/voice_probe.py` | the probe (self-contained; regenerates piper WAV if missing; `--no-publish`, `--hold`, `--total-timeout`, `--evidence-dir`; token redaction built in; `os.nice(10)` + overall watchdog ≤170 s) |
| `status/c4a-evidence/voice-probe-20260911-083131.jsonl` + `.summary.json` + `voice-probe-run2.console.log` | **canonical run 2** evidence (39 records) |
| `status/c4a-evidence/voice-probe-20260911-083019.jsonl` + `.summary.json` + `voice-probe-run1.console.log` | run 1 (same result; endpoint fra / connection `cow-great`) |
| `status/c4a-evidence/leftover-check.txt` | post-run voice-state emptiness check |

Rerun: `cd /home/agent/workspace/fluxer && nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/voice_probe.py --hold 5`
(Redaction rule baked in: any `token`/`e2ee_key` field anywhere in recorded payloads becomes `{redacted, len, prefix}`.)

## 4. Blockers

- **None blocking this probe.** No permissions blocker surfaced: CONNECT worked first try, SPEAK not suppressed, `canPublish: true`. (No `kairo` action needed — the voice channel existed and the bot can join + publish.)
- **Receive leg not testable headless** — needs a second participant (human or second account) publishing audio; then verify `track_subscribed` → `rtc.AudioStream(track)` decode + speaker events. Schedule with kairo/a human for wave 3 (5-minute test, this bot participates from the sandbox).

## 5. Unknowns / open items (for wave 3)

1. **Grant expiry at 600 s**: what happens mid-session — does Fluxer re-push `VOICE_SERVER_UPDATE`, does `token_refreshed` carry a usable long-term refresh, or must the bot re-op4 (new `connection_id`)? Probe sessions were ~18 s, so untested. Design for "re-op4 same channel on refresh/disconnect" as the safe strategy.
2. Host-only endpoint form (`ferret.iad.fluxer.media`) documented by the SDK notes was not observed live; both runs gave full `wss://` URLs. Probe handles both.
3. The fluxerjs "advanced / being reworked" warning (docs §6) still stands — keep the voice module thin so it can be swapped.
4. DM-call variant (`dm_channel_{channel_id}`, `guild_id: null`) untested — guild flow only.
5. `token_refreshed` semantics (see §2.9).
6. Non-fatal `_data_track` FFI log line — watch in wave 3; not seen to affect media.

## 6. Recommendation — wave 3 voice module (cascade)

**Shape**: `plugin-src/fluxer/voice/` — `controller.py` (join/leave/session lifecycle), `cascade.py` (the pipeline), `audio.py` (resample/ring buffers), plus config in `plugin.yaml`. Reuse `gatewayws.update_voice_state` (frozen interface already works) and this probe's redaction helper (hoist to `fluxer/_secrets.py` if C3/C4b both need it).

**Lazy import rule (required)**: `import livekit` **only inside** voice functions; catch `ImportError` → log once "voice disabled: `uv pip install livekit` into the Hermes venv", keep the plugin text-only. (livekit IS installed now, but a fresh venv must not break plugin load.)

**Cascade (v1)**: `track_subscribed` → `rtc.AudioStream(track)` (48k) → energy VAD / silence debounce → one utterance buffer → **whisper.cpp** (`gpu/tools/whisper-bin-ubuntu-x64/whisper-cli -m models/ggml-tiny.en.bin -t 4`, upgrade to base.en) → text into the adapter's normal message pipeline (source=voice) → agent reply → **piper** (`gpu/tools/piper/piper`, model path config) → PCM 48k → `AudioSource.capture_frame` publish. Single utterance at a time; drop concurrent speech; barge-in deferred (v2).

**Session binding / config points**: `voice.enabled`; `voice.channels` (guild→channel allow-list); `voice.session_binding: ephemeral|channel|invoke` (spec §6 wording); `voice.transcripts: off|on` (post STT text to the channel vs keep private); `voice.stt` (binary/model/threads); `voice.tts` (piper binary/model/voice); `voice.silence_ms`, `voice.max_utterance_s`; `voice.auto_leave` (empty room for N s → op4 null).

**Lifecycle**: one LiveKit room per bound channel; on room `disconnected` → bounded re-op4 rejoin; token strategy per §5.1; always leave via op4 null; never log tokens (probe's rule).
