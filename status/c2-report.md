# C2 report — Fluxer adapter, wave 1 (text scope)

Date: 2026-09-11 (08:15–08:30 UTC). Author: C2. Spec: `docs/spec-fluxer-plugin.md` §2.4.
All work under `/home/agent/workspace/fluxer/`; the Hermes checkout was read-only.

## 1. Deliverables

| Deliverable | Path | State |
| --- | --- | --- |
| Adapter (`FluxerAdapter` + `register()` + `interactive_setup()` + standalone sender + `env_enablement_fn`) | `plugin-src/fluxer/adapter.py` (stub replaced, 710 lines) | done |
| Unit tests | `plugin-src/fluxer/tests/test_adapter.py` + `tests/conftest.py` | 50 passed |
| Sandbox E2E driver | `scripts/e2e_text.py` (phases `send`, `check`, `wait-webhook-log`, `cleanup`, `selfmsg`, `sweep`, `inject`) | done |
| Plugin checks | `hermes plugins validate` / `doctor` on the plugin dir | both pass |
| Evidence | `status/c2-evidence/` (pytest, validate, doctor, inject, gw.log excerpt) | done |

## 2. What the adapter implements (spec §2.4)

- `connect()`: token from `get_scoped_secret("FLUXER_BOT_TOKEN")` → `GET /users/@me` → platform
  lock `_acquire_platform_lock("fluxer", <bot id>, "Fluxer bot <bot id>")` (after identity, per spec)
  → REST + `FluxerGatewayClient` → `start()` (waits READY) → `_wire_plugin_handlers(None)` →
  `_mark_connected()`. Failures: missing/invalid token → non-retryable fatal; network/deps →
  retryable fatal (gateway client's own `retryable` flag honored).
- `disconnect()`: WS stop → REST close → `_release_platform_lock()` → `_mark_disconnected()`.
- Inbound `MESSAGE_CREATE` filter chain, in order: self → `author.bot` + `extra["ignore_bots"]`
  (default true) → local authz mirror (`FLUXER_ALLOW_ALL_USERS` / `FLUXER_ALLOWED_USERS`, fail
  closed, one warning per user) → trigger policy (DM always; guild: `free_response_channels` →
  `/` command prefix → `<@id>`/`<@!id>` mention → `mention_patterns` regex → explicit
  `require_mention: false`) → bot-mention strip → `build_source` (`chat_type` `"dm"` /
  `"group"` matching the Discord convention; `scope_id`/`guild_id`, `message_id`) →
  `MessageEvent(TEXT)` → guarded `handle_message`.
  Verification log lines: `Fluxer: message from <user> in <chat> (trigger=<why>)` /
  `Fluxer: ignored message from <user> (<why>)`.
- Outbound `send()`: ≤4000-char chunks via `truncate_message` (newline-aware, code-fence safe,
  never mid-word), `message_reference` on the first chunk only (built from the explicit
  `reply_to`; `channel_id`/`guild_id` added only when metadata provides them); `SendResult`
  maps 5xx/429-exhausted → `retryable=True` (`transient`/`rate_limited`), other 4xx → not
  retryable; network exceptions retryable; `retry_after` surfaced. `send_typing` self-throttles
  ≥8 s per chat; `edit_message`/`delete_message` passthroughs; `get_chat_info` → `{name, type, chat_id}`.
- WS lifecycle: `disconnected` → `_mark_degraded()`; `reconnect_failed` → `_set_fatal_error(
  "gateway_reconnect_failed", retryable=True)` + `_notify_fatal_error()`; `ready`/`resumed` →
  `_mark_connected()`. Events during the initial connect are suppressed (connect() raises itself).
- `register(ctx)`: exact kwarg set — `name="fluxer"`, `label="Fluxer"`,
  `adapter_factory=FluxerAdapter`, passive `check_fn` (deps importable + token present),
  `ensure_deps_fn=None`, `validate_config`/`is_connected` (token contains `.`),
  `required_env=["FLUXER_BOT_TOKEN"]`, `setup_fn`, `env_enablement_fn`,
  `cron_deliver_env_var="FLUXER_HOME_CHANNEL"`, `standalone_sender_fn`,
  `allowed_users_env="FLUXER_ALLOWED_USERS"`, `allow_all_env="FLUXER_ALLOW_ALL_USERS"`,
  `max_message_length=4000`, `emoji="⚡"`, `pii_safe=False`, `allow_update_command=True`,
  `platform_hint`.
- `standalone_sender_fn`: REST-only chunked send, no WS; `media_files` via
  `FluxerREST.upload_attachment` when present, else a clear `{"error": ...}` (never fakes success).

## 3. Unit tests

```
cd /home/agent/.hermes/hermes-agent
./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_adapter.py -q
→ 50 passed in 3.01s
```

Coverage: filter-chain table (self / bot-flag accept+reject / unauthorized fail-closed + once-per-user
log / free-response / mention + strip / command prefix / mention_pattern / DM / group-DM /
`require_mention:false` / mention-only drop / source fields), mention-strip variants, chunking
(long text ⇒ ≥3 chunks, each ≤4000, no mid-word splits; newline split keeps paragraphs whole),
reply-reference first-chunk-only + no guessing, send success/not-connected/empty, error mapping
(503→retryable transient, 429→retryable rate_limited+retry_after, 403→not retryable forbidden,
reset→retryable), partial-chunk continuation ids, typing throttle (2 sends over simulated time),
edit/delete passthroughs, `get_chat_info` type mapping, connect order (`get_me → lock → ws → wire
→ mark_connected`, lock args asserted), missing-token fatal non-retryable, connect failure releases
lock, disconnect releases lock + closes transports, connection-event state transitions incl. the
initial-connect guard, `env_enablement` (None/`{}`/home channel/api-base overrides),
`validate_config`/`check_requirements`, `register()` kwarg smoke, standalone sender (success,
chunking, missing token, empty, error mapping, media unsupported → error, media upload → claim).
All tests use fake/mocked clients — no network.

## 4. Plugin checks

```
./venv/bin/python ./hermes plugins validate /home/agent/workspace/fluxer/plugin-src/fluxer  → exit 0, "Validation passed."
./venv/bin/python ./hermes plugins doctor   /home/agent/workspace/fluxer/plugin-src/fluxer  → exit 0,
    "OK: runtime discovery, manifest parsing, import, and registration passed"
```
(Full outputs: `status/c2-evidence/plugins-validate.txt`, `plugins-doctor.txt`.)

## 5. Sandbox E2E (live)

Sequence (all `HERMES_HOME=/home/agent/workspace/fluxer/sandbox/hermes-home`, never `--replace`):

1. Sandbox env: appended `FLUXER_ALLOW_ALL_USERS=true` to `sandbox/hermes-home/.env` (dev sandbox
   only). Needed because the adapter's local check is fail-closed AND the gateway authz
   default-denies with no allowlist — without it no external author could trigger the bot.
2. `bash scripts/sandbox_start.sh` → gateway log shows the plugin loaded as
   `hermes_plugins.fluxer_platform.adapter` and the adapter connect line:
   ```
   08:15:31 INFO hermes_plugins.fluxer_platform.adapter: Fluxer: gateway ready (1547828742208888832)
   08:15:31 INFO hermes_plugins.fluxer_platform.adapter: Fluxer: connected as Esther (1547828742208888832)
            — REST https://api.fluxer.app/v1, gateway wss://gateway.fluxer.app
   08:15:31 INFO gateway.run: Gateway running with 1 platform(s)
   ```
3. `scripts/e2e_text.py send` (webhook route — **blocked**):
   ```
   fluxer.rest.FluxerAPIError: HTTP 403 [MISSING_PERMISSIONS]: You don't have the permissions required to perform this action.
   ```
   Permission evidence: bot role `Esther` `permissions=0x2000000232ce80` (`MANAGE_WEBHOOKS` bit
   0x20000000 **not** set, `ADMIN` not set); `@everyone` also lacks it; channel has no
   permission overwrites; listing `/channels/{id}/webhooks` and `/guilds/{id}/webhooks` also 403.
   → The non-self-author live leg cannot be closed from a bot token. **Needs kairo to grant
   MANAGE_WEBHOOKS (or send a message himself).**
4. `scripts/e2e_text.py selfmsg` (live bounded probe instead): posts a marked message AS the bot;
   the running sandbox gateway receives the real MESSAGE_CREATE over its WS and the adapter's
   first gate drops it — live proof of gateway → adapter event delivery:
   ```
   08:16:26 INFO hermes_plugins.fluxer_platform.adapter: Fluxer: ignored message from Esther (self)
   ```
5. Standalone sender (cron path) — `hermes send --to fluxer:1547815091221561347 'standalone sender test'`
   (sandbox HERMES_HOME) → `sent`. Read-back via REST: message `1547883580644921344` in #general;
   gw.log shows the gateway also saw it over WS (`Fluxer: ignored message from Esther (self)`,
   08:16:43) → validates `standalone_sender_fn` + the live REST write path.
6. **Live agent reply through the sandbox gateway** (cron leg, closes the delivery hop): created a
   one-shot cron job in the sandbox home delivering to `fluxer:1547815091221561347`
   (`hermes cron create "1m" "Reply with exactly this one line ..." --name e2e-c2-live-reply
   --deliver fluxer:1547815091221561347 --repeat 1`). The running sandbox gateway executed the
   job and delivered the agent's reply through the **live adapter**:
   ```
   08:21:26 INFO cron.scheduler: Running job 'e2e-c2-live-reply' (ID: 4728a731d352)
   08:21:31 INFO cron.scheduler: Job 'e2e-c2-live-reply' completed successfully
   08:21:33 INFO hermes_plugins.fluxer_platform.adapter: Fluxer: ignored message from Esther (self)
   08:21:33 INFO cron.scheduler: Job '4728a731d352': delivered to fluxer:1547815091221561347
            via live adapter thread=- message_id=1547884796678508544
   ```
   Read back via REST in #general (full content):
   `'Cronjob Response: e2e-c2-live-reply\n(job_id: 4728a731d352)\n-------------\n\n[e2e] sandbox cron delivery via adapter.send\n\nTo stop or manage this job ...'`
   → a live agent turn was rendered by `FluxerAdapter.send()` in the sandbox gateway process and the
   adapter saw its own message come back over WS. (Nuance: the triggering turn came from cron, not
   from an inbound Fluxer message — the inbound non-self leg is still blocked, see step 3.)
   Message `1547884796678508544` deleted afterwards; cron job removed
   (`Removed job: e2e-c2-live-reply (4728a731d352)`). Evidence:
   `status/c2-evidence/gw-log-run2-cron-reply.txt`.
7. `scripts/e2e_text.py inject` (offline fallback, per task): a real captured payload
   (`docs/captures/event-20260911-075316-3-MESSAGE_CREATE.json`) with author/id rewritten to a
   fresh external user, fed through a standalone `FluxerAdapter` with a mock handler → the full
   pipeline produced one `handle_message` call:
   ```
   {"handle_message_calls": 1, "text": "[e2e] ping (offline injection)",
    "chat_id": "1547815091221561347", "chat_type": "group",
    "user_id": "9990000000000000002", "user_name": "e2e-external",
    "scope_id": "1547815091221561344", "message_id": "178911469067600"}
   INJECT PASS
   ```
   (Saved: `status/c2-evidence/e2e-inject.txt`; rerun anytime with `python scripts/e2e_text.py inject`.)

### Cleanup confirmation

```
scripts/e2e_text.py cleanup / sweep → deleted 1547883506686758912, 1547883580644921344
cron reply 1547884796678508544 → deleted; GET → 404 UNKNOWN_MESSAGE
hermes cron delete 4728a731d352 → "Removed job: e2e-c2-live-reply (4728a731d352)"
list_messages(#general) → 0 marker messages remaining; no webhook was ever created
bash scripts/sandbox_stop.sh → "✓ Stopped gateway for this profile"; sandbox pidfile removed
ps -p 1 → still the live gateway process (PID 1 untouched, no --replace anywhere)
```

## 6. Deviations / notes

- **Live reply leg blocked** (webhook 403, above). The `send`/`check`/`wait-webhook-log` phases
  are implemented and ready to run the day the bot has MANAGE_WEBHOOKS or kairo posts. This is
  the documented follow-up: “needs a real user message (kairo) to close”.
- Bot-flagged webhook messages would additionally be dropped by the `ignore_bots` lever (spec §2.4
  gate 2) — untested live because webhooks could not be created.
- Local authz mirror is a superset of the spec's two env vars: it also honors
  `GATEWAY_ALLOW_ALL_USERS` and `extra.allow_from`/`extra.allowed_users` (parity with the runner's
  authz gates) — still fail closed with no allow source.
- `tests/conftest.py`: I wrote the sys.path + `Platform("fluxer")` registration setup; a sibling
  had also written a file at that path (collision noted at write time). Content is additive and
  C1's test files are self-contained, so their suite collects fine. During my run window C1's
  `test_rest.py`/`test_gatewayws.py` were mid-edit (17 failures at 08:12, mtimes seconds old); by
  08:25 they pass (31 passed) — full plugin suite: 81 passed (50 mine + 31 C1's).
- Sandbox `.env` now carries `FLUXER_ALLOW_ALL_USERS=true` (dev sandbox; the live installation
  is unaffected). Sandbox `config.yaml` untouched.

## 7. Unknowns

- The **inbound non-self → agent reply** chain (a real external author's mention driving a reply)
  remains unproven end-to-end: webhook creation is permission-blocked (step 3), so the chain is
  covered as [real WS delivery] + [full pipeline via injected payload] + [live reply delivery via
  the gateway's adapter] — the only missing live hop is an external author's message triggering
  the turn. It closes the moment kairo grants MANAGE_WEBHOOKS / posts, or kairo sends a message;
  rerun `scripts/e2e_text.py send|wait-webhook-log|check|cleanup` then.
- DM inbound delivery for bots remains untested against the live server (documented unknown in
  `docs/fluxer-api-notes.md` §11).
