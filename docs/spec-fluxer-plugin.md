# Spec: `fluxer` plugin — Hermes chat platform adapter

**Separation note:** This document defines the **fluxer chat platform plugin only**. It connects fluxer.app (or any compatible messaging API) to Hermes — text messages, channels, attachments, events. The realtime voice/video/omni engine is a **separate project** defined in `docs/spec-roles-profiles.md`. The two are independent: fluxer works fine without the realtime engine, and the realtime engine serves any platform (discord, fluxer, telegram, etc.) that chooses to use it. Cross-reference: fluxer can optionally enable realtime features by loading the engine (see §5).

Sources of truth: `docs/fluxer-api-notes.md` (protocol), `docs/hermes-plugin-integration.md` (Hermes seams), `reference/fluxer-openapi.json`, `docs/captures/` (real payloads).

## 0. Ground rules (all coders)
- Write ONLY under `/home/agent/workspace/fluxer/`. The Hermes checkout (`/home/agent/.hermes/hermes-agent`) is READ-ONLY for this project (read it as a reference; never edit it).
- Python: `/home/agent/.hermes/hermes-agent/venv/bin/python` (3.11). Tests: run with `./venv/bin/python -m pytest <paths> -q` from the checkout, or straight from the plugin source directory with both the checkout root and the plugin package on `sys.path`.
- The live gateway is container PID 1 — never touch it. All `hermes gateway` commands MUST run with `HERMES_HOME=/home/agent/workspace/fluxer/sandbox/hermes-home`. Never pass `--replace`.
- Keep CPU light: nice -n 10 for live scripts; no compiles; no loops; bound everything.
- Live Fluxer writes: only clearly-marked test content; delete what you create; respect rate limits (sleep between calls). Token: read from `.env` files, never echo/hardcode.
- The sandbox gateway may be running during integration tests — don't run a second concurrent full bot session if avoidable; short overlapping probes are OK (mark your test messages distinctly).

## 1. Layout (wave 1)

```
/home/agent/workspace/fluxer/plugin-src/fluxer/     # the plugin package (symlinked into the sandbox)
├── plugin.yaml        # C2 owns (exact content below, §2.5)
├── __init__.py        # C2 owns: `from .adapter import register`
├── models.py          # C1
├── rest.py            # C1  (FluxerREST)
├── gatewayws.py       # C1  (FluxerGatewayClient)
├── adapter.py         # C2  (FluxerAdapter + register() + setup + standalone sender)
└── tests/             # C1: test_rest.py, test_gatewayws.py | C2: test_adapter.py
```

Scripts (workspace `/scripts/`): C1 → `client_selftest.py`; C2 → `e2e_text.py` (or documented manual steps). Evidence reports: `status/c1-report.md`, `status/c2-report.md`.

## 2. Frozen interfaces

### 2.1 `rest.py` — `FluxerREST`
Async aiohttp client for `https://api.fluxer.app/v1` (base URL configurable).

```python
class FluxerAPIError(Exception):
    status: int; code: str | None; message: str; retry_after: float | None  # from 429

class FluxerREST:
    def __init__(self, token: str, base_url: str = "https://api.fluxer.app/v1"): ...
    async def close(self) -> None: ...
    # core request (rate-limit aware): parses X-RateLimit-*; on 429 sleeps
    # Retry-After/retry_after (capped) and retries (max 3); other errors raise FluxerAPIError.
    async def request(self, method: str, path: str, *, json_body=None, params=None, files=None,
                      expected=(200, 201, 204)) -> Any: ...
    # convenience (all return parsed JSON unless noted):
    async def get_me(self) -> dict                          # GET /users/@me
    async def get_gateway_info(self) -> dict                # GET /gateway/bot
    async def get_channel(self, channel_id: str) -> dict
    async def get_guild(self, guild_id: str) -> dict
    async def list_guild_channels(self, guild_id: str) -> list[dict]
    async def list_messages(self, channel_id: str, *, limit: int = 50, before=None, after=None) -> list[dict]
    async def create_message(self, channel_id: str, *, content: str | None = None, embeds=None,
                             attachments=None, message_reference=None, allowed_mentions=None,
                             nonce=None) -> dict
    async def edit_message(self, channel_id: str, message_id: str, *, content=None, embeds=None) -> dict
    async def delete_message(self, channel_id: str, message_id: str) -> None   # 204
    async def send_typing(self, channel_id: str) -> None
    async def open_dm(self, recipient_id: str) -> dict      # POST /users/@me/channels
    async def upload_attachment(self, channel_id: str, file_path: str, *,
                                content_type: str | None = None, filename: str | None = None) -> dict
        # plan→PUT→(complete if multipart) → returns claim dict usable in create_message(attachments=[...])
        # {"id": 0, "filename", "upload_filename", "file_size", "content_type"}
        # singlepart ≤10 MiB; multipart >10 MiB via upload_id/parts//attachments/complete; bot cap 50 MiB.
```

- Auth header: `Authorization: Bot *** (only the REST side; gateway differs).
- Errors: map `{"code","message","errors"}` envelope to `FluxerAPIError`.
- Keep a small bucket map from `X-RateLimit-Bucket/Remaining/Reset-After` (best-effort pre-sleep); 429 handling is the must-have.
- No retry on 4xx (except 429); retry on 5xx/timeouts with backoff.

### 2.2 `gatewayws.py` — `FluxerGatewayClient`
WebSocket client for `wss://gateway.fluxer.app` (URL override supported).

```python
class FluxerGatewayClient:
    def __init__(self, token: str, *, url: str = "wss://gateway.fluxer.app",
                 on_event: Callable[[str, dict], Awaitable[None]],
                 on_connection_event: Callable[[str, dict | None], Awaitable[None]] | None = None): ...
    async def start(self) -> None    # connect → HELLO → IDENTIFY (raw token!) → wait READY;
                                     # spawns heartbeat + dispatch loops; returns after READY.
    async def stop(self) -> None     # graceful close, cancel tasks.
    @property def user_id(self) -> str | None: ...   # from READY user.id
    @property def session_id(self) -> str | None: ...
    @property def ready_payload(self) -> dict | None: ...
    async def update_voice_state(self, guild_id: str | None, channel_id: str | None, *,
        self_mute=True, self_deaf=True, self_video=False, self_stream=False) -> None  # op 4
    async def set_presence(self, status: str = "online") -> None                     # op 3
```

Behavior:
- Connect with `?v=1&encoding=json` (no compression). HELLO → heartbeat interval (default 41250 ms; use server value). First heartbeat with jitter; ack tracking; if no ack ~45 s → reconnect.
- IDENTIFY `{"op":2,"d":{"token":"<RAW>","properties":{"os":"Linux","browser":"hermes-fluxer","device":"hermes-fluxer"},"presence":{"status":"online","afk":false}}}` — no intents field.
- Dispatch op0 `{t, s, d}` → `on_event(t, d)` (await; catch+log per-handler errors so one bad event cannot kill the loop). Track seq for resume.
- Reconnect: on clean-ish drop, RESUME op6 `{token, session_id, seq}` within 60 s → wait RESUMED; if op9/failed → fresh IDENTIFY. Backoff 1,2,5,10,30,60 (cap). Emit connection events: `("ready", payload)` / `("resumed", None)` / `("disconnected", {"code":..., "reason":...})` / `("reconnect_failed", {...})` so the adapter can mark state / fatal errors.
- Close code 4004 (or identify rejection) → non-retryable signal via connection event.
- Send helpers must respect ≤4096-byte client frames (ours are tiny) and the 600/60 s budget trivially. Expose `send_raw(op, d)` for future use.
- `on_event` fires for ALL dispatch events (adapter routes).

### 2.3 `models.py` — helpers (small)
- `parse_message(d) -> dict` normalization (ensure keys; parse `timestamp`/`edited_timestamp` to `datetime`; convenience accessors), `parse_user`, `parse_channel` — keep thin; anything more is adapter-side. Also `AT_MENTION_RE` for `<@id>` / `<@!id>` detection.

### 2.4 `adapter.py` — `FluxerAdapter` (text scope)
Contract from `docs/hermes-plugin-integration.md` §5 + checklist. Text-scope requirements:

- `__init__`: `super().__init__(config=cfg, platform=Platform("fluxer"))`; read `cfg.extra` (keys §4). Token via `get_scoped_secret("FLUXER_BOT_TOKEN")` (fallback: `cfg` extra `token` is NOT used — token is env-only).
- `connect(is_reconnect=False)`: acquire platform lock via `self._acquire_platform_lock("fluxer", <bot_id>, "Fluxer bot <bot_id>")` AFTER fetching `/users/@me` (need bot id for lock identity). Create REST + WS client; `await client.start()`; register `self._wire_plugin_handlers(None)`; `self._mark_connected()`; return True. Failures → `_set_fatal_error(code, msg, retryable=...)` (missing/invalid token → non-retryable; network → retryable) + `_notify_fatal_error()` rule from base; return False.
- `disconnect()`: stop WS, close REST, `_release_platform_lock()`, `_mark_disconnected()`.
- Inbound: `MESSAGE_CREATE` → filters, in order:
  1. `author.id == self bot id` → drop.
  2. `author.bot` and `extra["ignore_bots"]` (default True) → drop. (Also skip controlled webhooks only if flagged bot; keep lever.)
  3. Author authorization: honor `allow...env` behavior — implement local check mirroring IRC (`FLUXER_ALLOWED_USERS`, `FLUXER_ALLOW_ALL_USERS`); fail closed (log once per user).
  4. Trigger policy: DMs (channel type 1/3) → always for authorized users. Guild → respond if channel in `free_response_channels` (extra) OR content mentions the bot (`<@id>`/`<@!id>`, plus `mention_patterns` extra) OR message starts with a command prefix (`/`). Respect `require_mention` extra (bool) if present and not free-response. Strip the bot mention from the text passed to the agent (keep content otherwise intact).
  5. Build source + event: `self.build_source(chat_id, chat_name, chat_type, user_id, user_name, scope_id=guild_id or None, message_id=...)`; chat_type: `"dm"` for DM/group-DM, else read discord adapter's value convention for guild text channels and follow it. `chat_name`: channel name (cache from REST lazily; fall back to id). `MessageEvent(text=..., message_type=TEXT, source=..., message_id=..., timestamp=...)` then `await self.handle_message(event)` (guard `self._message_handler`).
  6. Log lines for verification: `Fluxer: message from <user> in <chat> (trigger=<why>)` / `Fluxer: ignored message from <user> (<why>)`.
- Outbound: `send(chat_id, content, reply_to=None, metadata=None)` → chunk to ≤4000 (`splits_long_messages = True`, newline-aware, never split mid-word), `create_message` per chunk; `reply_to` → `message_reference` on first chunk (from metadata only if provided; do not guess). Return `SendResult(success=True, message_id=...)`; on error `success=False` with `retryable`/`error_kind` where known. `send_typing`: POST typing, self-throttle ≥8 s. `edit_message`/`delete_message`: thin passthroughs (used by future flows). `get_chat_info`: REST → `{"name", "type": "dm"|"channel"|..., "chat_id"}`.
- Fatal/connection events from WS: `disconnected` (retryable) → mark degraded + notify per base guidance; `reconnect_failed` after client gives up → `_set_fatal_error(retryable=True)` + `_notify_fatal_error`. `ready` after reconnect → mark connected again.
- `register(ctx)`: kwargs per checklist; `name="fluxer"`, `label="Fluxer"`, `check_fn` = deps importable + token present (passive!), `ensure_deps_fn=None` (aiohttp+websockets ship with Hermes), `validate_config`/`is_connected` = token present & well-formed (contains "."), `required_env=["FLUXER_BOT_TOKEN"]`, `setup_fn=interactive_setup`, `env_enablement_fn`, `cron_deliver_env_var="FLUXER_HOME_CHANNEL"`, `standalone_sender_fn`, `allowed_users_env="FLUXER_ALLOWED_USERS"`, `allow_all_env="FLUXER_ALLOW_ALL_USERS"`, `max_message_length=4000`, `emoji="⚡"`, `pii_safe=False`, `allow_update_command=True`, `platform_hint=` (dancer standard markdown ok; keep replies concise).
- `standalone_sender_fn(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False)`: REST-only send (no WS); replicate chunking; if `media_files` given → upload via `upload_attachment` (claim); return `{"success": True, "message_id": ...}` or `{"error": ...}`.
- `env_enablement_fn`: None if no token; else seed `{}` + `home_channel` from `FLUXER_HOME_CHANNEL` (`FLUXER_HOME_CHANNEL_NAME`), plus `api_base`/`gateway_url` overrides from env if set.

### 2.5 `plugin.yaml` (exact; adjust wording only)
```yaml
name: fluxer-platform
label: Fluxer
kind: platform
version: 1.0.0
description: |
  Fluxer gateway adapter for Hermes Agent.
  Connects to fluxer.app and relays messages between Fluxer guilds/DMs
  and the Hermes agent. Text + attachments. Voice/video features are
  provided by the realtime engine plugin (docs/spec-roles-profiles.md).
author: NousResearch
requires_env:
  - name: FLUXER_BOT_TOKEN
    description: "Fluxer bot token (id.secret)"
    prompt: "Fluxer bot token"
    password: true
optional_env:
  - name: FLUXER_ALLOWED_USERS
    description: "Comma-separated Fluxer user IDs allowed to talk to the bot"
    prompt: "Allowed users (comma-separated)"
    password: false
  - name: FLUXER_ALLOW_ALL_USERS
    description: "Allow any Fluxer user to trigger the bot (dev only)"
    prompt: "Allow all users? (true/false)"
    password: false
  - name: FLUXER_HOME_CHANNEL
    description: "Default channel ID for cron / notification delivery"
    prompt: "Home channel ID"
    password: false
  - name: FLUXER_HOME_CHANNEL_NAME
    description: "Display name for the Fluxer home channel"
    prompt: "Home channel display name"
    password: false
```

## 3. Config & env (read at construct time)
- `cfg.extra` keys: `free_response_channels` (list[str]), `ignore_bots` (bool, default True), `mention_patterns` (list[str] regexes), `require_mention` (bool), `api_base`, `gateway_url`. Shared bridged keys (free_response_channels, mention_patterns, require_mention, dm_policy, …) arrive via the config loader automatically — read from `extra` only.
- Env: `FLUXER_BOT_TOKEN` (secret), `FLUXER_ALLOWED_USERS`, `FLUXER_ALLOW_ALL_USERS`, `FLUXER_HOME_CHANNEL`, `FLUXER_HOME_CHANNEL_NAME`, optional `FLUXER_API_BASE`, `FLUXER_GATEWAY_URL`.

## 4. Testing & evidence
- Unit tests must cover: REST (mock aiohttp: auth header, error envelope, 429 retry, upload plan singlepart + multipart logic), gateway (mock WS server: hello→identify→ready, heartbeat, resume, dispatch routing, close-code signal), adapter (filters/policy table, chunking, send success/fail, env_enablement, register sanity).
- C1 live evidence: `scripts/client_selftest.py` → connects, prints READY user, sends ONE message `[selftest] fluxer client ok <ts>` to #general (1547815091221561347), captures its own MESSAGE_CREATE via event, edits it, deletes it; also tries one ~2500-char message to probe the 4000-char claim; deletes everything; writes `status/c1-report.md` with command + outputs.
- C2 live evidence: sandbox E2E — sandbox gateway (see scripts/sandbox_*.sh) with the plugin symlinked + enabled; trigger an inbound event as a non-self author (preferred: a webhook posting to #general — if webhook events don't arrive, inject a captured MESSAGE_CREATE payload from `docs/captures/` into the adapter's event handler and verify the full adapter path produces a reply via REST); verify the agent's reply appears in #general (read-back), then stop the sandbox + delete test messages. Write `status/c2-report.md` (commands, log excerpts, pass/fail, unknowns).
- Run `hermes plugins validate`/`doctor` on the plugin dir (sandbox HERMES_HOME for gateway commands; validate/doctor are path-based so no home needed).
- Acceptance for wave 1: unit tests pass; C1 selftest passes; C2 E2E produces a live agent reply via the sandbox; reports written; no leftover test messages; sandbox stopped.

## 5. Sandbox cheat sheet
- Start: `scripts/sandbox_start.sh` → tail `sandbox/gw.log`; expect a `Fluxer:` connect line.
- Status/stop: `scripts/sandbox_status.sh`, `scripts/sandbox_stop.sh`.
- Sandbox home: `sandbox/hermes-home` (config: fluxer enabled, free_response #general, plugins enabled=fluxer-platform). NEVER omit `HERMES_HOME` on gateway commands; NEVER `--replace`.

## 5. Real-time engine bridge

fluxer can optionally enable the realtime engine plugin (`docs/spec-roles-profiles.md`) for voice/video features. The bridge is simple:

- fluxer loads the realtime engine as a companion if and only if the config contains `features: [realtime]` (or a `voice.enabled` flag) and the engine's package is importable.
- When the realtime engine is active, fluxer's voice-state updates (`.update_voice_state()`) are mapped to engine session commands (join channel, leave channel, start feed, stop feed). The adapter at `gatewayws.py` already supports op4 for guild voice channels (transported over the platform's gateway). That opcode surface remains the same — the realtime engine consumes the raw stream once the bridge connects.
- The engine's `thinker` role resolves to the fluxer adapter's `handle_message()` (i.e. the Hermes agent), respecting the talker/thinker split in the active omni profile.
- Voice transcripts (if `transcripts: channel`) are delivered via the fluxer adapter's `create_message()`, using the same REST path as text messages.

For complete documentation of the realtime engine's profile system, role taxonomy, backend protocols, cancellation pathways, and session FSM, see `docs/spec-roles-profiles.md`.

## 6. Reports
Each coder writes `status/cN-report.md` (what done, evidence paths, exact commands, pass/fail, open issues) and ends with a short summary message: deliverables + evidence + blockers.