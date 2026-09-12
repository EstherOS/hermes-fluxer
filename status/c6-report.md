# C6 report — voice UX fixes (wave 5, fluxer lane)

**Date:** 2026-09-11 ~14:15–14:50 UTC · **Author:** C6 · **Scope:** `plugin-src/fluxer/` voice + adapter +
sandbox config/env + ASR server + unit tests.
**Verdict: ✅ all code fixes committed, tests green (261 passed), sandbox rebooted, ASR server live,
8 root causes identified and addressed.** Kairo should retest with `/voice join` from a server channel
and `/voice status` from DM.

---

## 1. Deliverables

| Path | What | Lines |
|---|---|---|
| `voice/config.py` | `qwen3asr_server` engine, `queue_utterance`, `input_prompt`, `stt.server_url`, `stt.fallback_engine`, `stt.timeout_s` — parsed with warn-on-bad defaults | +27 |
| `voice/cascade.py` | utterance single-slot queue; GPU ASR server STT engine + fallback; voice preamble on dispatched events; `_ENGINE_TRANSCRIBERS` registry | +95 |
| `voice/controller.py` | speak/speak_file queueing (single-slot, bounded wait); `find_user_voice_channel()` for DM cross-guild resolution | +33 |
| `adapter.py` | `_voice_input_prompt_for_chat()` + `_resolve_channel_prompt()`; `_handle_dm_voice_command()`; `_voice_bound_send` interim-metadata skip; `_handle_message_create` channel_prompt for voice-bound text; DM /voice join/leave interception | +107 |
| `scripts/asr_server_start.sh` | Idempotent GPU ASR server (port 8105, Vulkan, nice 10, health poll 30s) | 57 |
| `scripts/asr_server_stop.sh` | Stop via PID or pkill | 17 |
| `scripts/setup_sandbox_env.py` | +FLUXER_HOME_CHANNEL, +FLUXER_HOME_CHANNEL_NAME, +HERMES_GATEWAY_BUSY_ACK_ENABLED | — |
| `sandbox/config.yaml` | stt.engine: qwen3asr_server; escape hatch settings; heartbeat-free-response; display.platforms.fluxer quieting | — |
| `tests/test_voice.py` | 21 new tests (config, queue, ASR parse+fallback, interim-send, preamble, DM voice, find_user) — 261 total | +168 |
| `status/c6-evidence/` | ASR startup + transcription test + gw-load artifact | — |
| `status/c6-report.md` | this file | — |

## 2. Config table (new keys under `extra.voice.*`)

| Key | Default | Notes |
|---|---|---|
| `input_prompt` | see constant `DEFAULT_VOICE_INPUT_PROMPT` | Injected as ephemeral per-turn `channel_prompt` for any turn in a voice-bound chat. Empty string disables. |
| `queue_utterance` | `true` | Single-slot queue for inbound utterances (cascade) AND outbound speech (controller side). `false` restores v4 drop behavior. |
| `stt.engine` | `hermes` | Now also accepts `qwen3asr_server`. |
| `stt.server_url` | `http://127.0.0.1:8105` | Warm GPU llama-server endpoint. |
| `stt.fallback_engine` | `whispercpp` | Engine used when `qwen3asr_server` fails or times out. Empty string disables. Circular (== engine) silently disables. |
| `stt.timeout_s` | `60` | Per-call budget for the ASR server POST (5..300). |

## 3. Issues — root causes + fixes

| # | Reported symptom | Root cause | Fix |
|---|---|---|---|
| 1 | STT quality bad (whispercpp tiny.en → music tags junk) | whisper.cpp tiny.en hallucinates noisy/music audio → song lyrics | **Qwen3-ASR GPU server** (0.6B Q8_0, llama-server port 8105, ~0.36s warm latency). Falls back to whispercpp on server failure. |
| 2 | Agent didn't know replies are spoken (told user to "type it") | No voice-mode context injected into the prompt | `voice.input_prompt` default preamble: teaches the turn is a spoken call; reply will be TTS'd; write for the ear; never say "type". Tests verify injection. |
| 3 | Replies too long (7–14s WAVs) | Model wrote paragraphs; TTS spoke everything | **Prompt-level brevity only** (preamble says 1–3 short sentences). No hard truncation (documented; rely on prompt control). |
| 4 | Turn-taking jank — dropping utterance / dropping speech | Single-channel concurrency: both cascade and speak dropped while busy | `queue_utterance: true` (new default): inbound utterances single-slot-queued (latest wins), outbound speech waits bounded by `speak_timeout_s`. |
| 5 | Stuck session with pending clarify + 30min Working | Clarify tool blocks turn; answer via text resolves it. Busy acks + long-running heartbeat posted into channel. | **Sandbox restart** clears in-memory clarify state. **Config quieting** stops heartbeat posts + busy acks. Documented `/stop` or answering clarifies. |
| 6 | 8× "Normal final-send NOT suppressed" warnings | Stream consumer created but never delivered (fluxer platform not in `_PLATFORM_DEFAULTS` → global streaming default ON). Consumer existed → warning fired but only one send happened. | `display.platforms.fluxer.streaming: false` — kills stream consumer entirely (no warning, no preview noise). |
| 7 | "⏳ Working — 33 min — iteration 1, clarify" + "⚡ Interrupting current task" posts flooding voice channel | Long-running notification heartbeat (`⏳ Working`) + busy ack (`⚡ Interrupting`) sent via `adapter.send()` → voice send-hook → **spoken aloud** (and posted as text). | `long_running_notifications: false` kills heartbeat; `HERMES_GATEWAY_BUSY_ACK_ENABLED=false` (sandbox .env) kills busy acks. Additionally, **interim-metadata suppression**: `_interim_send`/`non_conversational` → posted as text but NOT spoken. |
| 8 | Heartbeat channel message ignored ("no trigger: mention required") | `free_response_channels` lacked the new channel (1547960647273156608). | **Added to config.** |
| 9 | "image didn't arrive" | kairo asked "Do you see?" — no MESSAGE_CREATE with attachments ever arrived at Fluxer in ANY channel (checked REST): the image never left his client. **Not our bug.** | Investigated and documented: zero attachment-bearing messages in history. Fluxer itself has no screen-share transport (DM call returns ACCESS_DENIED). Documented: attachments in voice text channels ARE processed normally (existing media pipeline). |
| 10 | No home channel warning | `FLUXER_HOME_CHANNEL` env was not set (only DM channel 15478308… needed). | Set in sandbox .env via `setup_sandbox_env.py` (static keys). |
| 11 | `/voice join` not working from DM | Core's `_handle_voice_channel_join` requires `event.raw_message.guild_id`, which DMs lack. | **Plugin intercepts** `/voice join\|channel\|leave` in DMs: uses `controller.find_user_voice_channel()` (cross-guild state lookup) to resolve VC; joins/leaves via the controller; replies in the DM. `/voice on\|off\|tts\|status\|bare` passes through to core. |

## 4. `/voice` command verification

### From a **guild text channel** (e.g. any server channel):

| Command | Behavior | Requirements |
|---|---|---|
| `/voice join` | Joins the voice channel where you (kairo) currently sit (resolved via VOICE_STATE_UPDATEs). | You must be in a voice channel. Bot must have Connect+Speak perms. |
| `/voice leave` | Leaves the guild's voice session. | Bot must be connected. |
| `/voice status` | Shows voice mode + channel name + participants. | Instant. |
| `/voice on` / `off` / `tts` | Toggles chat-level voice-reply mode. | Instant. |
| `/voice` (bare) | Toggles on/off, shows help. | Instant. |

### From a **DM**:

| Command | Behavior | Requirements |
|---|---|---|
| `/voice join` | Scans ALL tracked guilds for your current voice channel → joins in that guild. | You must be in a voice channel in any guild where the bot has tracked your state. |
| `/voice leave` | Leaves ALL active voice sessions (usually one). | Bot must be connected. |
| `/voice status` | Core handles: shows mode only (no channel info). | Instant. |
| `/voice on` / `off` / `tts` | Core handles normally (works across all chats). | Instant. |

### Auto-join / Auto-leave configuration:
- **Disable auto-join**: set `extra.voice.auto_channels: []` in config (or remove `- '154…1348'` entry). The `/voice join` command still works.
- **Set auto-leave**: set `extra.voice.auto_leave_after_s: 600` (leave after 10 min empty). Default 0 = off.

## 5. Evidence summary

| Artifact | Path & Description |
|---|---|
| ASR server live | `status/c6-evidence/asr-server-startup.log` — 5s ready on port 8105 |
| ASR transcription test | `status/c6-evidence/asr-transcription-test.log` — "Turn the lights on, please." at 0.36s latency, 0.8 word overlap |
| Clean gateway startup | `status/c6-evidence/gw-startup.log` — fluxer connected + room connected, 0 duplicate-send warnings |
| ASR server smoke reference | `docs/captures/omni-asr-server-evidence.md` (prior) |
| Channel message history | REST check: zero attachments in all channels; clarify message 1547965349494792192 retrieved |

## 6. Verified vs documented vs known gaps

**Verified:** Unit tests (261 pass), ASR server works with piper audio (real request), sandbox boots clean with ASR + voice auto-join, no duplicate-send warnings, config quieting applied.

**Documented but not live-tested** (await kairo): /voice commands from DM (plugin intercept), interim-speech suppression, voice preamble effect on model output (needs conversational retest), heartbeat channel reply, home channel delivery, /voice join while sitting in a VC (needs kairo's client-side voice).

**Known gaps / caveats:**
- ASR fallback whispercpp tiny.en may still hallucinate on noisy audio — acceptable; Qwen3-ASR is the primary engine.
- `voice.input_prompt` applies to ALL turns in a voice-bound channel (including typed text), not only voice-originated — intentional: any reply in a bound chat is spoken, so the model should know it's in-call.
- DM /voice join uses `find_user_voice_channel` scanning all guilds; if user sits in multiple VCs simultaneously (unlikely), the first found wins (alphabetical guild order).
- Silence_ms left at 900 ms (no evidence supports reducing; queueing solves dropped utterance).
- Future realtime/A2A profiles: must be opt-in and off-by-default (kairo's note — not this wave's work).

## 7. How to toggle items for retest

1. **Disable ASR (for fallback test)**: `bash scripts/asr_server_stop.sh` — then the cascade falls back to whispercpp with a log warning.
2. **Re-enable busy acks**: remove `HERMES_GATEWAY_BUSY_ACK_ENABLED=false` from sandbox .env → `bash sandbox_stop.sh && bash sandbox_start.sh`.
3. **Re-enable streaming previews**: set `display.platforms.fluxer.streaming: true` in config → restart.
4. **Force silence_ms change**: set `voice.silence_ms: 600` in config → restart.
5. **Quiet voice chat (no posts)**: set `voice.transcripts: off` → restarts. Replies still spoken; transcripts not posted.