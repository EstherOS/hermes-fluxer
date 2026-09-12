# Fluxer bot API — reference for the Hermes integration

Compiled 2026-09-11 (night shift R1). Scope: what a **bot token** can do on
`api.fluxer.app` (production instance), REST + WebSocket gateway + voice +
media, with exact shapes wherever verified.

**Evidence tags** — every claim carries one:

| Tag | Meaning |
| --- | --- |
| `(live)` | verified against the live instance tonight (raw captures in `docs/captures/`) |
| `(docs)` | https://docs.fluxer.app/<path> (fetched tonight) |
| `(openapi)` | `reference/fluxer-openapi.json` (downloaded copy of the live spec, 259 paths / 423 schemas) |
| `(sdk)` | fluxer.ts (github.com/zeroxs/fluxer.ts) or fluxerjs/core (github.com/fluxerjs/core) source |
| `(inferred-unverified)` | reasoning from the above, not directly observed |

Live-probe helper: `scripts/fluxer_probe.py` (subcommands `listen`, `send`,
`send-file`, `edit`, `delete`). Raw responses file names are cited as
`captures/<file>`.

---

## 0. TL;DR

- REST base `https://api.fluxer.app/v1`, header `Authorization: Bot <token>`.
  Bot token format: `<application_id>.<secret>` (no `flx_` prefix; that prefix
  is a *user* session token and is rejected by bot routes). `(live, openapi, docs)`
- **There is no intents system.** Identify takes `token` + `properties` only.
  Bots receive message content unconditionally for channels they can view.
  Traffic shaping is `ignored_events` (a deny-list of event names) + Lazy
  Request subscriptions. `(docs, live)`
- **There is no slash-command / interaction API.** No interaction endpoints in
  the 259-path spec, no `Interaction` schema in 423 schemas, no docs page, no
  interaction code in either SDK. Commands are prefix commands parsed from
  message text. `(openapi, docs, sdk)`
- Attachments: plan (`POST /channels/{id}/attachments`) → `PUT` bytes to the
  returned `upload_url` (its own capability, no auth header) → claim by sending
  a message with `attachments[].upload_filename`. ≤10 MiB singlepart; larger =
  multipart + `/attachments/complete`. Bot cap 50 MiB. `(docs, openapi, live)`
- Attachment download: the `url` on the message is public (no auth). Verified
  GET 200 with exact bytes and a `?format=webp&width=32` transform. `(live)`
- Voice is **LiveKit** (WebRTC), not a Fluxer voice socket: op-4 Voice State
  Update → `VOICE_SERVER_UPDATE {token, endpoint}` → LiveKit `room.connect`.
  Fluxer's own SDK joins, publishes and **receives** audio (Opus) and video.
  `(docs, sdk)`
- Gateway: `wss://gateway.fluxer.app?v=1&encoding=json`, heartbeat 41.25 s,
  60 s resume window, close codes 4000–4012. `(live, docs)`
- Rate limits: global 50 req/s per identity; per-route buckets like
  `channel:message:create::channel_id` 20/10 s; gateway send budget 600
  payloads/60 s per connection. `(docs, live)`

---

## 1. Auth & tokens

**REST** `(openapi: components.securitySchemes; docs: http-api/gateway, authentication)`

| Scheme | Header form | Used by |
| --- | --- | --- |
| `botToken` | `Authorization: Bot <token>` | bot routes; primary method |
| `sessionToken` | `Authorization: <token>` (no prefix) | user sessions |
| `oauth2Token` | `Authorization: Bearer <access_token>` | OAuth2 scopes (`identify`, `email`, `guilds`, `connections`, `bot`) |

- Live token: 63 chars, starts `154782` (its application id), contains a `.` —
  matching the documented bot-token shape *"a full stop that is neither its
  first nor last character, with decimal digits before it"* `(live, docs)`
- `GET /gateway/bot` accepts only the `Bot` prefix meaningfully: the bare and
  `Bearer` forms are IP-keyed and record an auth-failure signal. Always send
  `Bot`. `(docs)`
- **Gateway** IDENTIFY sends the **raw token, no prefix** (`"token":
  "<raw>"`). `(docs: gateway/commands#identify, verified live)` The SDKs pass
  the raw token too. `(sdk)`
- Error envelope (all routes): `{"code": "<APIErrorCode>", "message": ...,
  "errors": [ {path, code, message}... ]}`. `(openapi: Error)`
  - Missing credential → 401 `MISSING_AUTHORIZATION`; wrong format / rejected
    → 401 `INVALID_AUTH_TOKEN`; bot token on user-only route → 403
    `ACCESS_DENIED`; `flx_` token on bot route → 401 `INVALID_AUTH_TOKEN`.
    `(docs, live: bot got 403 ACCESS_DENIED on /channels/{id}/call and /rtc-regions)`
- 429 body: `{"code":"RATE_LIMITED","message":...,"global":bool,"retry_after":n}`.
  `(openapi, docs)`

## 2. REST endpoints for a bot — the ones we validated

Mined from the OpenAPI spec + docs; the checked rows were exercised with the
live bot token tonight (captures cited). All paths under `/v1`.

### Identity / discovery

| Route | Notes | Evidence |
| --- | --- | --- |
| `GET /.well-known/fluxer` | no `/v1`, no auth. Full instance discovery. Live values: `api_public=https://api.fluxer.app`, `gateway=wss://gateway.fluxer.app`, `media=https://fluxerusercontent.com`, `static_cdn=https://fluxerstatic.com`, `webapp=https://web.fluxer.app`; `features.voice_enabled=true`; captcha=hcaptcha | `captures/probe-wellknown.json` `(live)` |
| `GET /gateway/bot` | `{"url":"wss://gateway.fluxer.app","shards":1,"session_start_limit":{"total":1000,"remaining":999,"reset_after":14400000,"max_concurrency":1}}` — all four limit values are **constants** (no ledger) | `captures/probe-gateway_bot.json` `(live, docs)` |
| `GET /oauth2/applications/@me` | app object incl. `bot.flags`, owner, `bot_public`; for our bot: id `1547828742208888832`, name `Esther`, `bot_public=true`, owner `kairo` | `captures/probe-app_me.json` `(live)` |
| `GET /users/@me` | works with bot token; returns the **bot user** (discriminator, bot:true, traits:["premium"], mfa_enabled:true) | `captures/probe-users_me.json` `(live)` |

### Guilds / members

| Route | Notes | Evidence |
| --- | --- | --- |
| `GET /users/@me/guilds` | array of guilds the bot is in | `captures/probe-users_me_guilds.json` `(live)` |
| `GET /guilds/{guild_id}` | guild object; live guild "test" has system_channel_id=#general, owner=kairo | `captures/probe-guild.json` `(live)` |
| `GET /guilds/{guild_id}/channels` | channel objects incl. categories (`type` 4), text (0), voice (2) | `captures/probe-guild_channels.json` `(live)` |
| `GET /guilds/{guild_id}/members?limit&after` | limit 1–1000 (default 1); returns member objects; live len 2 (bot + kairo) | `captures/probe-guild_members.json` `(live, openapi)` |
| `GET /guilds/{guild_id}/members/{user_id}` / `.../members/@me` | single member | `(openapi)` |
| `POST /guilds/{guild_id}/members-search` | search by query | `(openapi/sdk)` |
| `PATCH /guilds/{guild_id}/members/@me` | set own nick (`{"nick": ...}`) | `(sdk)` |

Channel types observed/live documented: 0 text, 1 DM, 2 guild voice, 3 group
DM, 4 category, 5 guild link; 998 guild link extended `(live, sdk)`. Guild
voice channels are **text-bearing too** (pins/messages/slowmode) `(docs: voice)`.

### Messages

| Route | Notes | Evidence |
| --- | --- | --- |
| `POST /channels/{channel_id}/messages` | create; JSON or multipart. Rate `20/10 s` per user+channel. Emits MESSAGE_CREATE | `(openapi, docs; live)` |
| `GET /channels/{channel_id}/messages?limit&before&after&around` | list newest-first, limit 1–100, rate `100/10 s` | `captures/probe-channel_general_messages.json` `(live)` |
| `GET /channels/{channel_id}/messages/{message_id}` | fetch one, rate `100/10 s` | `(openapi)` |
| `PATCH /channels/{channel_id}/messages/{message_id}` | edit content/embeds/flags; rate `20/10 s`; returns full message w/ `edited_timestamp` | `(live)` |
| `DELETE /channels/{channel_id}/messages/{message_id}` | 204; rate `20/10 s` | `(live)` |
| `POST /channels/{channel_id}/messages/bulk-delete` | `{"message_ids":[...]}` up to 100, ≤14 days old, rate `10/10 s` | `(openapi, sdk)` |
| `POST /channels/{channel_id}/typing` | typing indicator, rate `20/10 s` `(docs cache)` | `(sdk)` |
| `GET /channels/{channel_id}/messages/pins` | `{"items":[...],"has_more":bool}` (SDK normalizes) | `(sdk)` |
| Reactions | `PUT/DELETE /channels/{cid}/messages/{mid}/reactions/{emoji}/@me`, `GET .../{emoji}/users`, delete-all variants | `(openapi, sdk)` |

**Create message — request body** `(openapi: MessageRequestSchema; docs: http-api/messages#create-message)`

| Field | Type | Notes |
| --- | --- | --- |
| `content` | string | effective max 2000; bots/webhooks take max(resolved, 4000) |
| `embeds` | `RichEmbedRequest[]` | ≤10 (`max_embeds_per_message`); needs EMBED_LINKS; SDK example uses `{type:'rich', title, description, color}` |
| `attachments` | `ClientUploadedAttachmentRequest[]` | pre-uploaded claim: `{id, filename, upload_filename, file_size, content_type, title?, description?, flags?, duration?, waveform?}`; ≤10 |
| `message_reference` | object | reply: `{"message_id": "...", "channel_id": "...", "guild_id": "..."}` (type defaults 0; type 1 = forward). Reply target must be DEFAULT/REPLY type |
| `allowed_mentions` | object | `{parse:["users","roles","everyone"], users:[id], roles:[id], replied_user:bool}` |
| `flags` | int | sendable set: SUPPRESS_EMBEDS 4, SUPPRESS_NOTIFICATIONS 4096, VOICE_MESSAGE 8192, COMPACT_ATTACHMENTS 131072 (others dropped) |
| `nonce` | string\|int | 1–32 chars; same-channel replay within window returns the original message (idempotency hook) |
| `sticker_ids` | snowflake[] | ≤3 |
| `tts` | bool | forced false without SEND_TTS_MESSAGES |

Multipart alternative (inline files, no pre-upload): send `multipart/form-data`
with `payload_json` + `files[N]` binary parts (+ `content` etc. overrides).
`(docs)` This is what fluxer.ts's `files` option builds. `(sdk)`

**Message response** `(openapi: MessageResponseSchema; live capture)` — key
fields: `id`, `channel_id`, `author` (partial user), `type` (0 DEFAULT … 19
REPLY), `flags`, `content`, `timestamp`, `edited_timestamp`, `pinned`,
`mention_everyone`, `tts`, `mentions[]`, `mention_roles[]`, `mention_channels?`,
`users?`, `embeds[]`, `attachments[]`, `stickers[]`, `reactions?`,
`message_reference?`, `message_snapshots?`, `nonce?`, `call?`,
`referenced_message?` (absent vs null matters). Gateway adds `channel_type`,
`guild_id?`, `member?`, `mention_here?`, `nicks?`.

Live MESSAGE_CREATE capture (`captures/event-20260911-075316-3-MESSAGE_CREATE.json`):
author `Esther` (bot), `type: 0`, `content` = full text sent, `channel_type: 0`,
`guild_id`, `member` present with `user` stripped to `{"id": ...}` (docs say the
account is in `author`). `(live)`

Live MESSAGE_UPDATE (`...-4-MESSAGE_UPDATE.json`) carried full message +
`edited_timestamp` + `guild_id` + `member`. `(live)`

Live MESSAGE_DELETE (`...-5-MESSAGE_DELETE.json`): `{id, channel_id,
guild_id, content, author_id, member}` — content present because deleted by
the author; moderation deletions omit content/author_id. `(live, docs)`

### DMs (private channels)

- Create/open DM: `POST /users/@me/channels` body `{"recipient_id": "<user>"}`
  → channel object (`type: 1`). Group DM: `{"recipients": [ids...]}` (≤49, requires
  CAPTCHA per openapi description; user-only bucket note in docs). `(openapi, docs, sdk)`
- List: `GET /users/@me/channels` — live shows one DM with kairo:
  `captures/probe-users_me_channels.json`. `(live)`
- Send into a DM like any channel: `POST /channels/{dm_channel_id}/messages`.
  `(openapi)`
- Fetch DM channel live: `captures/probe-channel_dm.json` (`type: 1`,
  `recipients: [kairo]`). `(live)`
- DM one-to-one sends also check the recipient's DM policy/relationship;
  denial 400 `CANNOT_SEND_MESSAGES_TO_USER`. `(docs)`
- Recipients/friend ops: `PUT/DELETE /channels/{cid}/recipients/{uid}`.
  Bots cannot send friend requests (`BOTS_CANNOT_SEND_FRIEND_REQUESTS`). `(docs)`
- ⚠️ **Did not test**: whether a **bot** receives MESSAGE_CREATE for its DMs
  (no DM writes were made — night policy). Channel-visibility filtering suggests
  yes for DMs the bot is a recipient of. Treat as unknown until tested (see §11).

### Other route groups worth knowing (openapi index)

- Webhooks: full CRUD + execute `/webhooks/{id}/{token}` (+ `/github`,
  `/slack`, `/instatus` sinks), `?wait=true` returns the message `(sdk)`.
- Search: `POST /search/messages`; bulk fetch `POST /channels/messages/bulk`.
- Pins: `PUT/DELETE /channels/{cid}/pins/{mid}`, `GET .../messages/pins`.
- Scheduled messages, saved messages ("memes"), read states, invites
  (`POST /channels/{cid}/invites` → `GuildInvite`), audit logs, emojis,
  stickers, roles, bans — all present `(openapi)`; not exercised tonight.

## 3. Attachments — upload & download (exact mechanism)

**The mechanism is presigned PUT URLs, plus an upload relay.** Not multipart to
the API, not base64. `(docs: topics/uploads, media-proxy/upload-relay; openapi)`

Step-by-step (verified end-to-end live, see captures):

1. **Plan** — `POST /channels/{channel_id}/attachments`
   body `{"attachments":[{"id":0,"filename":"x.png","file_size":67,"content_type":"image/png"}]}`
   (1–10 items; `id` is a client-chosen int). Needs SEND_MESSAGES + ATTACH_FILES.
   Rate: 10/10 s per user+channel (shared with complete).
2. **Response** — one item per declaration, discriminated by `upload_mode`:
   - `singlepart` (≤ 10 485 760 bytes): `{id, filename, upload_filename,
     file_size, content_type, upload_mode:"singlepart", upload_url}`.
   - `multipart` (> 10 MiB): adds `upload_id`, `part_size` (ceil(size/20)
     rounded up to MiB, min 10 MiB), `parts:[{part_number (1-based),
     upload_url}]`.
3. **Transfer** — `PUT` the exact bytes to each `upload_url`. The URL's query
   string **is** the authorization (S3 signature or relay capability `t`);
   send **no** `Authorization` header. Singlepart: send `Content-Type` matching
   the declared type (relay ignores it; direct storage signs the byte count).
   Multipart: send each part's bytes only.
4. **Complete (multipart only)** — `POST /channels/{channel_id}/attachments/complete`
   body `{"uploads":[{"upload_filename":..., "upload_id":...}]}` → returns
   finalized `upload_filename`s.
5. **Claim** — `POST /channels/{channel_id}/messages` with
   `attachments:[{"id":0,"filename":...,"upload_filename":...,"file_size":...,
   "content_type":...}]` (+`content`, etc.). A claim against an object that was
   never PUT returns 400 `INVALID_FORM_BODY` + `FILE_NOT_FOUND`.

Live result of our round-trip (67-byte PNG): plan singlepart → PUT HTTP 200 →
message created with
`"attachments":[{"id":"1547877857999462400", ..., "url":"https://fluxerusercontent.com/attachments/1547815091221561347/1547877857999462400/fluxer_probe_pixel.png", "proxy_url": <same>, "width":1,"height":1, "content_hash":"a1ab…", "placeholder":"IAhC…", "expires_at":"2029-09-10T07:53:59…"}]` `(live)`

> Note: only the **singlepart** path was exercised live (small file). The
> multipart path (`upload_id`/`parts`/`/attachments/complete`) is
> docs-verified only — the probe implements it, but it has not run yet.

Limits: bot credential is **clamped to 50 MiB** (`52428800`) regardless of the
resolved user limit (default non-premium 25 MiB / premium 500 MiB, file
`max_attachment_file_size`); over → 400 `FILE_SIZE_TOO_LARGE` at plan time.
≤10 000 parts. `(docs)`

**Download** — attachment `url`/`proxy_url` is on the media host, **no auth**:
live `GET https://fluxerusercontent.com/attachments/{channel_id}/{attachment_id}/{filename}`
→ 200 `image/png`, byte-identical; with `?format=webp&width=32` → 200
`image/webp` 38 bytes. After the message was deleted the same URL 404'd —
attachment lifetime tracks the message. `(live)` Media proxy reads never take
the `Authorization` header; path is the authorization. `(docs)`

Transforms/selectors and caching: see §5. Attachment deletion route:
`DELETE /channels/{cid}/messages/{mid}/attachments/{aid}`. `(openapi)`

## 4. Application / slash commands — **none exist**

Checked exhaustively tonight:

- OpenAPI spec: 259 paths — no `interactions`, `commands`, or `application-commands`
  anywhere; 423 schemas — no Interaction/Command schema. `(openapi)`
- docs.fluxer.app: no interactions page; the gateway "Client commands" page is
  about protocol opcodes (identify/heartbeat/…), not app commands. `(docs)`
- fluxer.js.org / fluxerjs/core: command code = `parsePrefixCommand` on message
  text. `(sdk)`
- fluxer.ts: no interaction types; events only go up to reaction/typing. `(sdk)`
- Implementation implication: the Hermes adapter should implement **prefix
  commands** (e.g. `!new`, or mention/reply-triggered commands) by parsing
  `content` of MESSAGE_CREATE. There is no command registration, no callback
  endpoint/token, no deferred-response dance.
- There is a `POST /unfurl` ("Debug") and webhook executions, but no
  bot↔application command surface. `(openapi)`

**INTERACTION_CREATE does not exist on this platform** (no schema, no docs, no
SDK handler). Treat any future appearance as a new feature to re-probe. `(openapi, docs, sdk)`

## 5. Media endpoints ("media transformation and delivery")

Base URLs from instance discovery `(live)`: `media = https://fluxerusercontent.com`,
`static_cdn = https://fluxerstatic.com`. No `/v1` prefix on reads. `(docs: media-proxy/overview)`

- Route families: `/attachments/{path}`, `/external/{signature}/{target}`,
  `/themes/{path}.css`, `/entrance-sounds/{user_id}/{file}`, image assets
  (`/avatars`, `/icons`, `/banners`, `/emojis`, `/stickers`, guild member
  variants `/guilds/{gid}/users/{uid}/avatars`…), static objects `/{key}`. `(docs)`
- **Auth: none on public reads.** Object is authorized by its path; external by
  signature; uploads by URL capability. `(docs, live)`
- **Transformations** via query selectors (`format`, `width`, `height`,
  quality, animated flags per docs `media-proxy/transformations`); verified
  `format=webp&width=32` live. Response cache `max-age=31536000`. `(live, docs)`
- No request-count rate limit; ranges `bytes=…` supported (206/416). `(docs)`
- Upload relay: `PUT {media-base}/v1/relay/{key}?t=<capability>` — HMAC-signed
  capability payload `{b bucket, k key, m:"put", ct?, mb max-bytes, e expiry}`;
  900 s default expiry; 500 MiB default body limit; `ETag` on success; the
  client must use `upload_url` verbatim (never rebuild). `(docs)`
- Stream previews (Go-Live thumbnails): `POST /streams/{stream_key}/preview`
  (base64 JPEG ≤1 MB) / `.../preview/upload-url`; user-only. `(openapi, docs)`

## 6. Voice

**Transport: LiveKit (WebRTC).** Fluxer publishes no voice signalling protocol
of its own — no voice websocket, no voice opcodes, no UDP discovery, no custom
handshake. `(docs: voice, gateway/events#voice-server-update)`

Flow to join (guild channel):

1. `Voice State Update` (Gateway op 4): `{guild_id, channel_id, self_mute,
   self_deaf, self_video, self_stream, mutation_id?, base_version?}` — omit
   `connection_id` to open a new connection. DM calls: `guild_id: null`. `(docs)`
2. Server replies to the requesting session: `VOICE_SERVER_UPDATE`
   `{token, endpoint, connection_id, channel_id, guild_id?, e2ee_key?}` —
   `token` is a **LiveKit access token**, `endpoint` a `wss://` LiveKit URL
   (SDK notes bots may get host-only like `ferret.iad.fluxer.media`). `(docs, sdk)`
   Broadcast: `VOICE_STATE_UPDATE` to every session that can see the channel.
   With `mutation_id`, the requester also gets `VOICE_STATE_ACK`
   (`applied|rejected` + `error_code`). `(docs)`
3. Client joins LiveKit room `room.connect(endpoint, token)` — **one LiveKit
   connection carries mic, camera and screenshare**; the grant lists allowed
   sources (`SPEAK` → mic; `STREAM` → camera + 2 screenshare sources). Room
   name `guild_{guild_id}_channel_{channel_id}` (or `dm_channel_{channel_id}`);
   participant identity `user_{user_id}_{connection_id}`; **grant lifetime 600 s**. `(docs)`
4. Media: Opus audio. **Receive is supported** — the official-style SDK
   subscribes per participant and emits decoded `audioFrame` (Int16 PCM,
   `sampleRate`, `channels`) plus `speakerStart/speakerStop` and video frames;
   receive path uses `@livekit/rtc-node` + opus decoder (`opus-decoder` /
   `prism-media`); playback input expects **WebM/Opus**. `(sdk: fluxerjs/core packages/voice, README, voice-bot example)`
5. Playback/publish in the SDK: `connection.play(url)`; `stop()`;
   `voiceManager.leave(guildId)`; `serverLeave` event for reconnects. `(sdk)`

⚠️ The fluxerjs voice guide states: *"Advanced / being reworked… Do not build
production bots on the current voice APIs."* `(sdk/docs)` Cross-check the
LiveKit path after Fluxer's rework lands.

**REST bits around voice**

| Route | What | Evidence |
| --- | --- | --- |
| `GET /channels/{id}/rtc-regions` | region list `[{id,name,emoji}]`; **bot → 403 ACCESS_DENIED** live even on a guild voice channel; SDK says user-account only; alternative: `rtc_regions` array arrives in **READY** (live: 14 regions + `automatic`) | `(live, sdk, docs)` |
| `GET/PATCH /channels/{id}/call` | DM/group-DM call eligibility / region; **user-only** (bot 403 live) | `(live, docs)` |
| `POST /channels/{id}/call/ring` / `stop-ringing` / `call/end` | DM call control; user-only | `(openapi, docs)` |
| `POST /voice/channels/{id}/entrance-sound` | play entrance sound for callers; emits `ENTRANCE_SOUND_PLAY` | `(openapi, docs)` |
| `POST/DELETE /channels/{id}/voice-presence/heartbeat` | voice presence heartbeat | `(openapi)` |
| `POST /channels/{id}/voice-debug-logging/events`, `GET/PUT .../session` | debug logging | `(openapi)` |
| `PATCH /guilds/{gid}/members/{uid}` | moderator mute/deafen/move/disconnect (shared with member edit) | `(docs: voice)` |

Capacity/permission facts: `VIEW_CHANNEL`+`CONNECT` required; `SPEAK` gates mic
publish (else admitted with `suppress:true`); `STREAM` gates camera+Go Live;
camera cap 25 users; `user_limit`, `voice_connection_limit` (default 5, ≤100)
enforced; moving to another channel moves the connection via `connection_id`.
`(docs)`

## 7. Rate limits

**HTTP** `(docs: topics/rate-limits; live headers)`

- Global: **50 requests/second** per identity (account+credential kind; IP for
  anonymous), 1 s window. `HIGH_GLOBAL_RATE_LIMIT` accounts 1200/s; bypass flag
  exempts. Route bucket is charged only after the global admits.
- Per-route buckets, e.g.: list messages 100/10 s, get message 100/10 s,
  **create message 20/10 s**, edit 20/10 s, delete 20/10 s, bulk delete
  10/10 s, typing 20/10 s, attachment plan+complete 10/10 s shared,
  send phone… etc. Bucket key = route (+path resource) + identity.
- Headers seen live on a bot success response `(live)`:
  `X-RateLimit-Bucket: 668bcb75babe0f52`, `X-RateLimit-Limit: 60`,
  `X-RateLimit-Remaining: 59`, `X-RateLimit-Reset: <unix>`,
  `X-RateLimit-Reset-After: 0.167`. On 429 additionally `X-RateLimit-Scope`,
  `X-RateLimit-Global`, `Retry-After`; body per §1.
- Slowmode: 400 `SLOWMODE_RATE_LIMITED` (non-bot senders only; bots exempt). `(docs)`

**Gateway** `(docs: gateway/limits-and-rate-limits)`

- Send budgets: **600 client payloads per rolling 60 s per connection**, 600 per
  fixed 60 s per **session**, 6 000 per 60 s per source IP → exceed = close
  `4008 Rate limited`. (Not Discord's 120/60 — Fluxer is 600/60.)
- 256 concurrent sockets per IP; Identify budget 300 per 60 s per IP (excess
  silently discarded, no reply).
- Presence Update: 5 per rolling 20 s per socket; Voice State Update: 2/s
  immediate, then queue (64 deep, drains 1/500 ms, coalesces same target).
- Request Guild Members full-list: 1 accepted per guild per 30 s (bot);
  bounded requests: max 4 concurrent, 10 s deadline, excess dropped.
- Replay buffer: 4 096 events / 16 MiB; single event >2 MiB delivered but not
  retained; session resumable 60 s after a drop.

## 8. Permissions & bot flags

- Permission bitfield (SDK constants `(sdk: fluxer.ts types.ts)`; docs confirm
  names): CREATE_INSTANT_INVITE 0x1, KICK 0x2, BAN 0x4, ADMIN 0x8, MANAGE_CHANNELS
  0x10, MANAGE_GUILD 0x20, ADD_REACTIONS 0x40, VIEW_AUDIT_LOG 0x80,
  PRIORITY_SPEAKER 0x100, STREAM 0x200, VIEW_CHANNEL 0x400, SEND_MESSAGES 0x800,
  SEND_TTS 0x1000, MANAGE_MESSAGES 0x2000, EMBED_LINKS 0x4000, ATTACH_FILES 0x8000,
  READ_MESSAGE_HISTORY 0x10000, MENTION_EVERYONE 0x20000, USE_EXTERNAL_EMOJIS 0x40000,
  CONNECT 0x100000, SPEAK 0x200000, MUTE_MEMBERS 0x400000, DEAFEN_MEMBERS 0x800000,
  MOVE_MEMBERS 0x1000000, USE_VAD 0x2000000, CHANGE_NICKNAME 0x4000000,
  MANAGE_NICKNAMES 0x8000000, MANAGE_ROLES 0x10000000, MANAGE_WEBHOOKS 0x20000000,
  MANAGE_EXPRESSIONS 0x40000000, USE_EXTERNAL_STICKERS 0x2000000000,
  MODERATE_MEMBERS 0x10000000000, CREATE_EXPRESSIONS 0x80000000000,
  PIN_MESSAGES 0x8000000000000, BYPASS_SLOWMODE 0x10000000000000,
  UPDATE_RTC_REGION 0x20000000000000.
  Docs additionally reference `VIEW_CHANNEL_MEMBERS` (channel count APIs) and
  `BYPASS_SLOWMODE`; ADMINISTRATOR bypasses all checks. `(openapi: docs pages)`
- Bot flags (`(docs: http-api/applications#bot-flags)`): `FRIENDLY_BOT = 16`
  (accepts friend requests), `FRIENDLY_BOT_MANUAL_APPROVAL = 32`. Application
  `bot.flags` is the account's public-user flags bitfield; update via
  `PATCH /oauth2/applications/{id}/bot` (`bot_flags` field). `(openapi: BotProfileUpdateRequest)`
- DM rules: bots can create/open DMs and post in them; group-DM create requires
  captcha; friend-requests from bots refused; user-only surfaces (call control,
  rtc-regions, some settings) return 403 `ACCESS_DENIED` for bot tokens. `(openapi, docs, live)`

---

## 9. Gateway protocol (Deliverable B)

### Connection

- URL: `wss://gateway.fluxer.app` (from `GET /gateway/bot` or discovery);
  **append** `?v=1&encoding=json`. Optional compression
  `&compress=zstd-stream&stream=1` — continuous zstd stream both directions
  (server level 3); default: none. `(docs, live connect verified)`
- `v` is required: any other/absent value closes `4012` before Hello. `(docs)`
- Framing: one WebSocket message = one payload; **inbound (client→server) bound
  4 096 bytes** on the wire and after decompression (closes 4002). Server can
  send much larger frames. `(docs)`

### Handshake / lifecycle

1. Server sends `HELLO` op 10 `{heartbeat_interval: 41250}`.
2. Client sends `IDENTIFY` op 2:

```json
{"op": 2, "d": {
  "token": "<RAW TOKEN, no Bot prefix>",
  "properties": {"os": "Linux", "browser": "<lib>", "device": "<device>"}
}}
```

   Optional `d` fields: `presence` (status/afk/mobile), `ignored_events`
   (≤256 names, upper-cased+deduped), `flags` (session flags; bit 1 =
   DEBOUNCE_MESSAGE_REACTIONS), `initial_guild_id`, `shard` ([id,count]).
   **There is no `intents` field; the server ignores unknown fields.** `(docs, live)`
3. Server `READY` (dispatch, s=1): `session_id`, `version:1`, `user` (private
   repr), `guilds: []` for bots (guilds arrive as a GUILD_CREATE burst
   immediately after), `private_channels`, `rtc_regions` (ordered; live: 14 +
   `{id:"automatic"}`), `country_code`, `sessions`, `read_states`, settings,
   etc. `(docs, live: captures/event-*-1-READY.json)`
4. Heartbeat op 1 `d = last seq (or null)` every 41 250 ms (first beat with
   jitter). Server acks op 11; server may request an immediate beat via op 1
   after ~37.1 s without ack; no ack by 45 s → close `4009`. `(docs, live: acks observed)`
5. Dispatch op 0: `{op:0, t:"EVENT", s:<int>, d:{...}}`. `(live)`
6. Resume op 6 `{token, session_id, seq}` on a fresh socket within 60 s →
   replay + `RESUMED`; else op 9 `d:false` → identify again. Op 7 `Reconnect`
   → open new socket. `(docs)`

Live verification: full READY → GUILD_CREATE → MESSAGE_CREATE/UPDATE/DELETE
cycle captured with a minimal Identify (no intents field, no ignored_events).
`captures/listen-20260911-075316.jsonl` `(live)`

### Opcodes `(docs: gateway/opcodes-and-close-codes; matches sdk)`

| op | Name | Dir |
| --- | --- | --- |
| 0 | Dispatch | S→C |
| 1 | Heartbeat | both |
| 2 | Identify | C→S |
| 3 | Presence Update | C→S |
| 4 | Voice State Update | C→S |
| 5 | Voice Server Ping | reserved (never sent; sending = close) |
| 6 | Resume | C→S |
| 7 | Reconnect | S→C |
| 8 | Request Guild Members | C→S |
| 9 | Invalid Session (`d:false` only) | S→C |
| 10 | Hello | S→C |
| 11 | Heartbeat ACK | S→C |
| 12 | Gateway Error | reserved |
| 14 | Lazy Request | C→S |
| 15 | Request Guild Counts | C→S |
| 16 | Request Channel Member Counts | C→S |

### Close codes `(docs)`

4000 unknown error/drain · 4001 unknown opcode · 4002 decode/compression/
validation · 4003 not authenticated · 4004 invalid token · 4005 already
authenticated · 4007 invalid sequence · 4008 rate limited/too many sessions/
connections · 4009 heartbeat timeout · 4010 invalid shard · 4011 sharding
required (>2500 guilds/shard) · 4012 invalid API version. 4006 unassigned.
Session retention after close: 60 s (resumable), presence goes offline after 5 s.

### Key dispatch events (shapes)

All event payloads on docs.fluxer.app/gateway/events. Bot-relevant:

| Event | Shape summary | Evidence |
| --- | --- | --- |
| `READY` | see above | `(live, docs)` |
| `GUILD_CREATE` (burst after READY, also on join) | `{id, properties:{guild}, roles[], channels[], emojis[], stickers[], members[] (own member + voice participants; user stripped to `{id}` in the burst), member_count, online_count, voice_states[], joined_at}` | `(live: captures/event-*-2-GUILD_CREATE.json; docs)` |
| `MESSAGE_CREATE` | full message + `channel_type`, `guild_id?`, `member?` (user stripped), `mention_here?`, `nicks?` | `(live, docs)` |
| `MESSAGE_UPDATE` | full message + `guild_id?` + `member?`; needs READ_MESSAGE_HISTORY (or msg newer than cutoff) | `(live, docs)` |
| `MESSAGE_DELETE` | `{id, channel_id, guild_id?, content?, author_id?, member?}`; moderation deletions omit content/author_id | `(live, docs)` |
| `MESSAGE_DELETE_BULK` | `{ids[], channel_id, guild_id?}` | `(docs)` |
| `TYPING_START` | `{channel_id, user_id, timestamp (unix s), guild_id?, member?}`; delivery: active guilds, or small guilds (≤250), or `typing` override; suppressible per-guild / via `ignored_events` | `(docs)` |
| `MESSAGE_REACTION_ADD/REMOVE/…` | `{user_id, channel_id, message_id, emoji:{name,id?,animated?}, guild_id?, member?, session_id?}`; acting session excluded in guilds when request had `session_id` | `(docs)` |
| `VOICE_STATE_UPDATE` | voice state object (guild_id, channel_id|null, user_id, connection_id, session_id, mute/deaf/self_*, self_video/self_stream, suppress, viewer_stream_keys[], e2ee_capable, version) | `(docs)` |
| `VOICE_SERVER_UPDATE` | `{token (LiveKit), endpoint, connection_id, channel_id, guild_id?, e2ee_key?}` — requesting session only | `(docs)` |
| `VOICE_STATE_ACK` | outcome of own op-4 with `mutation_id`: `{mutation_id, runtime_epoch, connection_id?, guild_id?, channel_id?, status: applied\|rejected, server_version, canonical_state, error_code?}` | `(docs)` |
| `CALL_CREATE/UPDATE/DELETE` | DM call lifecycle (`channel_id`, `message_id`, `region`, `ringing[]`, `voice_states[]`, …) | `(docs)` |
| `GUILD_MEMBERS_CHUNK`, `GUILD_MEMBER_LIST_UPDATE`, `GUILD_COUNTS_UPDATE`, `CHANNEL_MEMBER_COUNTS_UPDATE` | responses to ops 8/14/15/16 | `(docs)` |
| `GUILD_AUDIT_LOG_ENTRY_CREATE` | captured live for message deletes (`action_type` 73 observed alongside message-delete options; see capture) | `(live)` |
| `INTERACTION_CREATE` | **does not exist** (see §4) | `(openapi, docs, sdk)` |

### Event filtering (the "intents" replacement) `(docs: gateway/event-filtering)`

- Four gates: guild availability → permission/visibility → guild subscription
  (passive session in >250-member guild gets a fixed subset) → session filters
  (`shard`, `ignored_events`).
- **A bot session is never passive** — it gets the full guild event stream it
  can see. Shaping levers: `ignored_events` deny-list and (per-guild) Lazy
  Request `typing` override.
- `MESSAGE_CREATE` overrides both the passive filter and `ignored_events` when
  the message *directly mentions* the bot user, `@everyone`, or `@here`.
- A suppressed event consumes no sequence number (no gaps).
- Removing the old Discord-style intent concern: **message content is always
  present**; there is no MESSAGE_CONTENT privilege. `(docs, live)`

## 10. Capture inventory (`docs/captures/`)

REST probes (all read-only): `probe-wellknown.json`, `probe-gateway_bot.json`,
`probe-app_me.json`, `probe-users_me.json`, `probe-users_me_guilds.json`,
`probe-guild.json`, `probe-guild_channels.json`, `probe-guild_members.json`,
`probe-channel_general.json`, `probe-channel_general_messages.json`,
`probe-channel_rtc_regions.json` (403), `probe-channel_dm.json`,
`probe-users_me_channels.json`, `probe-channel_dm_call.json` (403).

Gateway runs (raw JSONL + per-event files):
- `listen-20260911-075135.jsonl` — smoke test (READY, GUILD_CREATE)
- `listen-20260911-075316.jsonl` — **the controlled write sequence**:
  MESSAGE_CREATE (text) → MESSAGE_UPDATE → MESSAGE_DELETE → MESSAGE_CREATE
  (attachment) → MESSAGE_DELETE, plus 3× GUILD_AUDIT_LOG_ENTRY_CREATE.
  Per-event files `event-20260911-075316-*.json`.
- `listen-20260911-075224.jsonl` — extra READY/GUILD_CREATE sample.

Write sequence used (all cleaned up; the 3 messages created were each deleted):
1. `send` #general → captured MESSAGE_CREATE (content intact, no intents).
2. `edit` → captured MESSAGE_UPDATE.
3. `delete` → captured MESSAGE_DELETE (with content, author_id).
4. `send-file` tiny PNG → captured MESSAGE_CREATE w/ attachment; public GET of
   `url` (no auth) → 200; `?format=webp&width=32` → transformed; then deleted.

## 11. Unknowns / open items

1. **Bot receiving DMs** — cannot confirm until kairo wakes or a second account
   writes; no DM writes were made tonight. `(unknown)`
2. **Slash commands** — none today; re-probe periodically (spec diff) in case
   the platform ships them. `(openapi, negative finding)`
3. **Voice from Python** — LiveKit Python SDK (`livekit-rtc`) should be the
   equivalent of `@livekit/rtc-node`, but is untested here; the fluxerjs guide
   also flags voice as being reworked. `(inferred-unverified)`
4. **rtc-regions as a bot** — 403 on both a text and a voice channel; the
   READY `rtc_regions` array is the known-good source. Unknown whether some
   bot permission unlocks the route. `(live, unknown)`
5. **`VIEW_CHANNEL_MEMBERS` bit value** — referenced in docs, not in the SDK
   constant table; exact bit unknown. `(unknown)`
6. **Rich embed request schema** — only partially enumerated
   (`RichEmbedRequest` in openapi; SDK shows `{type:'rich', title, description,
   color}`). Not exercised live. `(openapi, sdk)`
7. **Media transform selector names** — verified `format` + `width`; docs page
   `media-proxy/transformations` has the full set (not fully read). `(docs/to-read)`
8. **Nonce idempotency window length** — docs say "that window" without a
   number read; safe to rely on same-channel replay returning the first
   message but verify before depending on it. `(docs)`
9. **Attachment `expires_at` semantics** — live value was ~3 years out; what
   triggers "expired" (deletion? moderation?) not investigated. `(live, unknown)`
10. **Rate limit bucket for `POST /channels/{id}/messages`** — docs say 20/10 s;
    live header sample was for the channels-list route (60/s window shown as
    `limit: 60`). Always trust `X-RateLimit-*` headers at runtime. `(docs, live)`

---

### Appendix: probe script usage

```
/home/agent/.hermes/hermes-agent/venv/bin/python scripts/fluxer_probe.py listen [seconds]
/home/agent/.hermes/hermes-agent/venv/bin/python scripts/fluxer_probe.py send <channel_id> <text>
/home/agent/.hermes/hermes-agent/venv/bin/python scripts/fluxer_probe.py send-file <channel_id> <path> [text]
/home/agent/.hermes/hermes-agent/venv/bin/python scripts/fluxer_probe.py edit <channel_id> <message_id> <text>
/home/agent/.hermes/hermes-agent/venv/bin/python scripts/fluxer_probe.py delete <channel_id> <message_id>
```

Env knobs: `FLUXER_ENV_FILE`, `FLUXER_API_BASE`, `FLUXER_GATEWAY_URL`,
`FLUXER_IGNORED_EVENTS`, `FLUXER_IDENTIFY_EXTRA` (JSON merged into Identify d),
`FLUXER_CAPTURE_DIR`. The token is read from `.env` (never printed); REST uses
`Bot <token>`, the gateway Identify uses the raw token.
