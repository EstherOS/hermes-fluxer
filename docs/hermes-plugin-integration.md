# Hermes Platform Plugin Integration Guide (for `fluxer`)

**Scope.** Everything below was verified on 2026-09-11 against the checkout at
`/home/agent/.hermes/hermes-agent` (venv `./venv/bin/python`, Python 3.11). Line numbers are
from that tree. The live gateway is that checkout's PID 1
(`.../venv/bin/python .../hermes gateway run`, `HERMES_HOME=/home/agent/.hermes`).

**Verification legend:** ✅ = read from code and/or exercised live during this research.

---

## 0. TL;DR — the two operational answers

**Q1 — Can platform plugins be (re)loaded without restarting the gateway?**
**No. Plugin loading is startup-only in the gateway process.** There is no SIGHUP/watcher/slash-command
reload path for plugins (details + evidence in §2). Enabling/disabling a plugin or dropping new adapter
code takes effect on the **next gateway process start**. There is an in-process
`discover_and_load(force=True)` API, but nothing in the running gateway calls it with `force=True`
(§2.3), and even a force re-scan would not re-create/reconnect already-created adapters.

**Q2 — How to run a SECOND gateway for testing without touching the live one?**
Give it its own `HERMES_HOME` (everything identity-related is home-scoped) and a distinct bot
credential. Verified working recipe in §12:
`env -i HOME=/home/agent PATH=... HERMES_HOME=/home/agent/workspace/fluxer/sandbox/hermes-home .../venv/bin/python .../hermes gateway run -v`
→ own `gateway.pid` / `gateway.lock` / `gateway.sock` / `state.db` / `sessions/`; live PID 1 untouched
(✅ smoke-tested, stopped cleanly with `hermes gateway stop` under the same `HERMES_HOME`).
**Never** pass `--replace` and never run `hermes gateway stop|restart` **without** the sandbox
`HERMES_HOME` — see §12.4.

---

## 1. Where plugins live and how they load

### 1.1 Discovery directories (in order; later wins on key collision)

`hermes_cli/plugins_discovery.py:134-160` (`collect_directory_manifests`):

| # | Directory | `source` | Notes |
|---|-----------|----------|-------|
| 1 | `<checkout>/plugins/` (top-level, minus `memory/`, `context_engine/`, `platforms/`, `model-providers/`) | `bundled` | `plugins_discovery.py:147-149` |
| 2 | `<checkout>/plugins/platforms/` | `bundled` | where **fluxer** belongs; `:150` |
| 3 | `$HERMES_HOME/plugins/` | `user` | live: `/home/agent/.hermes/plugins/` (currently only `hermes-talk`); sandbox: `$SANDBOX/plugins/`; `:151-153` |
| 4 | `$PWD/.hermes/plugins/` | `project` | only if `HERMES_ENABLE_PROJECT_PLUGINS=1`; `:154-159` |

Installed entry-point plugins (`hermes_agent.plugins` group) are a 5th source for pip-installed
packages (`plugins_discovery.py:40-68`) — not needed for fluxer.

Manifest parsing: flat dir `<root>/<name>/plugin.yaml` → registry **key = the manifest `name:` field**;
category dir `<root>/<cat>/<name>/plugin.yaml` → key `cat/name` (`plugins_manifest.py:458-471`;
`manifest_key()` `:316-318`). Portable `plugin.json` packages also supported (`:121-125`) — ignore.

### 1.2 What makes a plugin enabled/disabled

- `config.yaml` → `plugins.enabled: [...]` is an **allow-list; plugins are opt-in**
  (`plugins_discovery.py:90-99`). A missing key means "nothing enabled yet".
- `config.yaml` → `plugins.disabled: [...]` **wins over enabled** (`:80-87`, gate order `:192-193`).
- `~/.hermes/plugins_state.py` is **not** the enable/disable store — it is a per-plugin JSON
  key/value state store at `$HERMES_HOME/plugin-data/<ns>/state.json` (`plugins_state.py:107-168`).
  (The enable state lives only in config.yaml.)
- **Bundled platform plugins do NOT need `plugins.enabled`**: `gate_manifest()` returns `defer` for
  `source=="bundled" and kind=="platform"` *before* the enabled check
  (`plugins_discovery.py:205-212`). They register a lazy loader that only imports the adapter module on
  first lookup ("eagerly importing ~20 heavy SDKs added seconds to every `hermes` invocation", `:209-212`).
- **User/project plugins do need `plugins.enabled`** (`:213-217`); with the key present they are
  imported eagerly during discovery.
- A disabled/missing plugin's platform name is then rejected by the `Platform` enum, and
  `config.yaml` blocks for unknown platform names are **silently dropped**
  (`gateway/config_loader.py:163-168`; `gateway/config.py:676-686` "unknown platforms skipped").
  Failure mode = no error, no adapter, nothing in `gateway status`. Check `hermes plugins list`.

### 1.3 The `Platform` enum accepts dynamic names

`gateway/config.py:198-265`: `Platform._missing_` accepts a name iff
(a) a bundled dir `<checkout>/plugins/platforms/<name>/` exists with `__init__.py` + `plugin.yaml|yml`
(scan **cached** in `_Platform__bundled_plugin_names`), or (b) it is registered in the
`platform_registry` for the current `HERMES_HOME` scope.  Pseudo-members are cached so
`Platform("fluxer") is Platform("fluxer")`.

### 1.4 Load sequence in the gateway (what actually happens at startup)

1. `gateway/run_startup.py:865-871` `_start_register_plugins_relay_hooks()` → `discover_plugins()`.
2. `gateway/run_startup.py:909` `self.hooks.discover_and_load()` (shell hooks).
3. Plugin `register(ctx)` runs → `ctx.register_platform(...)` populates the registry
   (`hermes_cli/plugins.py:770-799`; `gateway/platform_registry.py:271-288`). Registration is
   **scoped per resolved `HERMES_HOME`** (`_scoped_entries`, `platform_registry.py:108-136`).
4. Adapters are created once in `_start_prefilter_platforms()` (`gateway/run_startup.py:945-990`):
   for every `enabled` platform in config, `self._create_adapter(platform, cfg)`;
   `_create_adapter` consults `platform_registry.create_adapter(name, cfg)` **first**, then built-ins
   (`gateway/run_adapters.py:1434-1460`; `platform_registry.py:349-383` — runs passive `check_fn`,
   then active `ensure_deps_fn` if needed, then `validate_config`, then `adapter_factory(cfg)`).
5. Failed connects are queued for background retry with backoff (`gateway/run_adapters.py:200-236`,
   `:766+` `_install_reconnected_adapter`). Every retry rebuilds the adapter from the **startup**
   config + registry entry, not from re-read plugin code.
6. `load_yaml_layer()` calls `discover_plugins()` itself (`gateway/config_loader.py:358-368`) before
   platform-name resolution, so config.yaml platform blocks resolve for user plugins too.

### 1.5 What a plugin directory must contain

```
plugins/platforms/fluxer/            # or $SANDBOX/plugins/fluxer (user plugin)
├── __init__.py                      # REQUIRED: exports register; e.g. `from .adapter import register`
├── plugin.yaml                      # REQUIRED: kind: platform (+ name/label/…)
└── adapter.py                       # your code (any layout; __init__.py is the import root)
```

- `__init__.py` is mandatory: `_load_directory_module` raises `FileNotFoundError` without it
  (`hermes_cli/plugins_loader.py:431-434`). IRC reference: `plugins/platforms/irc/__init__.py` is
  exactly `from .adapter import register`.
- The module is imported as `hermes_plugins.<slug>` (slug from key, `/`→`__`, `-`→`_`;
  `plugins_loader.py:412-421`) with the plugin dir on `submodule_search_locations`, so relative imports
  inside the plugin work.
- `register(ctx)` is called after import; exceptions are caught, the plugin's registrations are rolled
  back, and the plugin is recorded with an error (`plugins_loader.py:301-337`). `register()` must be
  import-light and side-effect-free beyond `ctx.*` calls.
- The bundled-dir `Platform` scan (`config.py:253-263`) also requires `__init__.py` + a manifest.

---

## 2. Q1 — hot (re)load: definitive answer + evidence

### 2.1 Answer

**No hot reload of platform plugins in a running gateway.** A new/edited plugin (or an
enable/disable change) only takes effect when the gateway process restarts. The running gateway also
won't pick up new config-platform entries or new plugin code on its own.

### 2.2 Evidence (all in-tree)

- Discovery runs once at startup and is memoized: `PluginManager.discover_and_load(force=False)`
  returns immediately when `self._discovered` is set (`hermes_cli/plugins.py:1202-1207`); only
  `force=True` unloads + rescans (`:1208-1209`, `unload()`/`unload_plugins` at `:2083`).
- No `force=True` caller anywhere in the gateway path. Repo-wide callers: `hermes_cli/main_dashboard.py:529`
  (dashboard auth setup, non-gateway), tests, and docs. Gateway startup calls the plain
  `discover_plugins()` only (`gateway/run_startup.py:868-871`, `:909`; idempotent no-op afterwards).
- Signals: the gateway installs handlers for **SIGINT/SIGTERM (shutdown)** and **SIGUSR1 (restart
  request)** only — `gateway/run.py:5235-5238`. **SIGHUP is not handled** (default action terminates;
  its only in-tree mention is crash diagnostics `gateway/shutdown_forensics.py:25`). SIGUSR1 =
  full process restart via `request_restart`, not a plugin reload (`run.py:5030-5033`, `gateway/restart.py`).
- Gateway slash commands reload *skills* and *MCP*, never plugins: `/reload-skills`
  (`gateway/slash_commands.py:1038`), `/reload-mcp` (`:1004`). No `/reload-plugins` command exists
  (handler list `:288-1204`).
- The CLI says it explicitly:
  - `hermes plugins install …` prints "Restart the gateway for the plugin to take effect: `hermes gateway restart`"
    (`hermes_cli/plugins_cmd.py:751-752`).
  - `hermes plugins enable` prints "Takes effect on next session." (`plugins_cmd.py:1014`).
  - Dashboard messaging router: "…is disabled. Enable it, then restart the gateway."
    ("…Restart the gateway to connect this platform.") (`hermes_cli/web_routers/messaging.py:890-900`).
  - Dashboard "apply config" flows call a full gateway restart helper
    (`hermes_cli/web_server_gateway.py:364-380` `_restart_gateway_after`).

### 2.3 What does exist (and its limits)

- `PluginManager.discover_and_load(force=True)` — in-process unload-all + full re-discovery + re-import
  (`plugins.py:1202-1238`), used by tests and `hermes_cli/main_dashboard.py:529`. It updates
  registrations but **does not** recreate/reconnect already-created adapters or re-read
  `config.yaml` platforms in a running gateway.
- Adapter-level reconnect exists (dropped connections are rebuilt and reconnected with backoff,
  `gateway/run_adapters.py:700-760, 766`), but always from the startup config/registry entry.
- In-chat `/restart` (`gateway/slash_commands.py:500`) and `hermes gateway restart` do a
  **process** restart (detached replacement watcher: `hermes_cli/gateway.py:934-944`), which *does*
  reload plugins — but it is a restart, not a reload.

### 2.4 Exact procedure to apply plugin changes

1. Install/place the plugin (bundled dir or enabled user plugin).
2. Restart the gateway process: `hermes gateway restart` (service installs) / `hermes gateway run --replace`
   (foreground/containers), or the in-chat `/restart`.
3. **In this container:** the live gateway is PID 1 — `hermes gateway stop|restart` with the default
   `HERMES_HOME` kills the container's main process. For development, use the sandbox gateway (§12).
   The safe way to make the *live* gateway pick the plugin up is an operator decision (container
   restart policy). `hermes gateway stop|restart` also refuse to run from inside a supervised gateway
   (`hermes_cli/gateway.py:5881-5890`), but that guard keys on env + PID-file ownership and does not
   protect you here — treat it as unsafe.

---

## 3. `plugin.yaml` schema

Parsed by `hermes_cli/plugins_manifest.py` (`parse_manifest_file:458-485`; known fields set
`:33-40`; kinds `:28`; dataclass `:321-370`). Unknown fields only warn (debug for v1) — `:156-162`.
`manifest_version: 2` enables the v2 fields; v1 files remain supported (`:117-124`).

**Fields that matter for a platform plugin:**

| Field | Type | Effect |
|---|---|---|
| `name` | str | Plugin id/key for a flat dir (used by `plugins.enabled`, `hermes plugins list`). Required. |
| `label` | str | Human label (config UI, status). |
| `kind` | str | Must be **`platform`** for a gateway adapter (`:28,436-455`). Undeclared kind is auto-detected; a platform dir should declare it explicitly. |
| `version`, `description`, `author` | str | Display only. |
| `requires_env` | list | Required env vars; surfaced in `hermes config` UI. Entries: bare `NAME` or dict `{name, description, prompt, url, password, category}` (IRC/Discord examples). Validation: must be UPPER_SNAKE (`plugin_validate.py:157-178`). |
| `optional_env` | list | Same shape; optional vars. |
| `pip_dependencies` / `python_dependencies` | list | **Validated + surfaced only — never auto-installed** (`:358-361`). |
| `requires_hermes` | str | Version gate, e.g. `">=0.19"` (`:412-419`). |
| `requires_plugins` | list | Advisory load ordering (`:356-357`, `:202-248`). |
| `provides_tools` / `provides_hooks` | list | Used by `hermes plugins doctor`/`validate` capability checks (`plugin_validate.py:319-363`). |
| `config_schema` | map | Schema for `plugins.entries.<id>.settings`; mismatch warns only (`:362-363,170-199`). |
| `capabilities` | list | Consent layer (grants required for some surfaces); keep empty unless needed (`:347-350`). |

**`requires_env` / `optional_env` do two things:**
1. Surfaced in the `hermes config` / setup UI. The injector reads each bundled
   `plugins/platforms/*/plugin.y(a)ml` at import of `hermes_cli/config.py` and merges entries into
   `OPTIONAL_ENV_VARS` (`hermes_cli/config.py:3746-3794` `_inject_platform_plugin_env_vars`).
   Names already hardcoded win; `password` is auto-inferred from a `*_TOKEN/_SECRET/_KEY/_PASSWORD/_JSON`
   suffix unless `password: false` (`:3780-3783`). Note: this scan is `get_project_root()/plugins/platforms`
   — **bundled only**; a user-dir plugin's vars won't auto-surface here.
2. Nothing else. They do **not** gate loading. Runtime gating is done by your `check_fn` and
   `validate_config` / `is_connected` callbacks (§4), and by env auto-enablement (§9.3).

Example skeleton (modeled on `plugins/platforms/discord/plugin.yaml`):

```yaml
name: fluxer-platform
label: Fluxer
kind: platform
version: 1.0.0
description: Fluxer gateway adapter for Hermes Agent.
author: NousResearch
requires_env:
  - name: FLUXER_BOT_TOKEN
    description: "Fluxer bot token"
    prompt: "Fluxer bot token"
    password: true
optional_env:
  - name: FLUXER_ALLOWED_USERS
    description: "Comma-separated Fluxer user IDs allowed to talk to the bot"
    password: false
  - name: FLUXER_HOME_CHANNEL
    description: "Default channel ID for cron / notification delivery"
    password: false
```

---

## 4. `register(ctx)` → `ctx.register_platform(...)`

Authoritative definition: `hermes_cli/plugins.py:769-799` (`PluginContext.register_platform`) +
`gateway/platform_registry.py:41-99` (`PlatformEntry`). Signature:

```python
ctx.register_platform(
    name="fluxer",               # config.yaml identifier; Platform("fluxer")
    label="Fluxer",
    adapter_factory=FluxerAdapter,      # (PlatformConfig) -> BasePlatformAdapter
    check_fn=check_requirements,        # PASSIVE "deps importable NOW" probe (must be side-effect free)
    validate_config=..., is_connected=..., required_env=[...], install_hint="", **entry_kwargs)
```
Unknown `**entry_kwargs` keys raise `TypeError` (`:779`). Live example to copy:
`plugins/platforms/irc/adapter.py:599-627`.

**Every supported kwarg / `PlatformEntry` field** (`platform_registry.py:41-99`):

| Field | Type / default | Semantics |
|---|---|---|
| `name` | str, required | config identifier; must equal `Platform` value used in config.yaml. |
| `label` | str, required | display name. |
| `adapter_factory` | `(cfg) -> adapter` | called by `create_adapter()`; construct there, connect later. |
| `check_fn` | `() -> bool` | passive dependency probe. Runs from status displays too — **never install here**. |
| `ensure_deps_fn` | `() -> bool`, None | active installer; runs ONLY from `create_adapter()` when `check_fn` is False. `None` = a False `check_fn` is a hard block. (`:52-64`) |
| `validate_config` | `(cfg) -> bool`, None | post-enable config check; None → let `connect()` fail descriptively. |
| `is_connected` | `(cfg) -> bool`, None | "configured?" for status/setup; falls back to `validate_config`, else `check_fn`. |
| `required_env` | list[str] | `hermes setup` display only. |
| `install_hint` | str | shown when `check_fn` is False. |
| `setup_fn` | `() -> None`, None | `hermes gateway setup` flow (IRC's `interactive_setup`). |
| `source` | "plugin" (set for you) | — |
| `plugin_name` | set from manifest | enables auto-enable from `hermes gateway setup`. |
| `allowed_users_env` | str | **env var name** for comma-separated user allow-list; read by authz (`gateway/authz_mixin.py:388,498`; startup minting `gateway/run_startup.py:831-857`). |
| `allow_all_env` | str | env var name for the truthy allow-everyone switch. |
| `max_message_length` | int, 0 | advertised cap (smart-chunking); 0 = no limit. Also read back by adapters via `platform_registry.get(name).max_message_length` (IRC convention, `irc/adapter.py:134-139`). |
| `pii_safe` | bool, False | session descriptions redact PII (`gateway/session.py:199,368`). |
| `emoji` | str "🔌" | CLI/gateway display. |
| `allow_update_command` | bool, True | whether `/update` is allowed from this platform (`gateway/slash_commands.py:1210-1220`). |
| `platform_hint` | str | injected into the system prompt as the platform's default hint (`agent/system_prompt.py:384-395`); user-overridable via `platform_hints.<platform>` config. |
| `env_enablement_fn` | `() -> dict \| None` | seeds `PlatformConfig.extra` (and optional `home_channel` dict) from env BEFORE adapter construction; `None` = not minimally configured → not auto-enabled (§9.3). |
| `apply_yaml_config_fn` | `(yaml_cfg, platform_cfg) -> dict \| None` | translate your config.yaml keys → env/extra; runs during `load_yaml_layer` after shared-key loop (`gateway/config_loader.py:300-307`). May set `os.environ` (guard with `not os.getenv(...)` for env>YAML). |
| `cron_deliver_env_var` | str | `*_HOME_CHANNEL` env var for cron `deliver=fluxer` (§9.4). |
| `parse_target_ref_fn` | `(ref) -> (chat_id, thread_id) \| None` | native target syntax before channel-directory fallback. |
| `validate_target_ref_fn` | `(ref) -> bool \| str` | post-resolution target validation; `str` = reject + diagnostic. |
| `send_message_handler` | `(args, normalized_chat_id, platform_name, pconfig)` | whole-request delivery override (sync/async). |
| `standalone_sender_fn` | `async (pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False) -> {"success": True, "message_id": …} or {"error": str}` | out-of-process sender for cron without a live gateway (§9.4). |

**Other `ctx.register_*` capabilities** (all in `hermes_cli/plugins.py` unless noted) — useful later,
not needed for a first platform cut: `register_tool` `:449`, `register_cli_command` `:638`,
`register_command` `:652` (in-session `/name`, usable from chat; conflicts with built-ins are refused),
`register_hook` `:893`, `register_middleware` `:897`, `register_system_prompt_section` `:917`,
`register_skill` `:974`, `register_platform_handler` `:820` (wire native SDK handlers in `connect()`),
`register_auxiliary_task` `:844`, `register_context_reference` `:712`, `register_source` `:1073`
(secret sources), `register_approval_transport` `:423`, `register_dashboard_auth_provider` `:740`.

---

## 5. `BasePlatformAdapter` contract

Class: `gateway/platforms/base.py:1772`. Only **three** `@abstractmethod`s plus `get_chat_info`:

| Method | Location | Required? | Contract |
|---|---|---|---|
| `__init__(config, platform)` | `:1816` | yes | call `super().__init__(config=cfg, platform=Platform("fluxer"))`; parse `cfg.extra`/env here. |
| `connect(*, is_reconnect=False) -> bool` | `:2388-2392` | **abstract** | return True on success; set fatal errors on failure; must be idempotent-safe for reconnects. |
| `disconnect()` | `:2394-2396` | **abstract** | close sockets, cancel tasks, release the platform lock. |
| `send(chat_id, content, reply_to=None, metadata=None) -> SendResult` | `:2398-2401` | **abstract** | text only; chunk if `splits_long_messages` else the router truncates (`:1796`). |
| `get_chat_info(chat_id) -> {name,type,chat_id}` | `:4158-4161` | **abstract** | `type` ∈ "dm"/"group"/"channel". |
| `send_typing(chat_id, metadata=None)` | `:2541` | recommended | typing heartbeat; `stop_typing` `:2544`; base `_keep_typing` loop `:2918`. |
| `edit_message` / `delete_message` | `:2412/:2420` | optional | default "not supported"; needed for streaming/edit flows. |
| `send_image/send_animation/send_voice/send_video/send_document/send_image_file/send_multiple_images` | `:2604/:2611/:2651/:2728/:2735/:2767/:2568` | optional | defaults send a "can't send media" notice (`:2659-2669`). Override what Fluxer supports. |
| `send_draft`, streaming TTS hooks | `:1926`, `:2695-2726` | optional | default = unsupported. |
| `create_handoff_thread` | `:2406` | optional | CLI→platform thread handoff. |

Useful class attributes: `supports_code_blocks` `:1777`, `supports_status_text` `:1779`,
`supports_async_delivery` `:1794`, `splits_long_messages` `:1796`,
`typed_command_prefix` `:1798` (set `"!"` if the client eats a leading `/`; the gateway accepts it —
same mechanism as Discord), `supports_inchannel_continuable` `:1802`, `interactive_resume` `:1811`,
`MAX_MESSAGE_LENGTH` read via `max_message_length_for_chat()` (4096 default) `:1878-1882`.

**Inbound flow (what your adapter calls):**
1. Build a source: `self.build_source(chat_id, chat_name, chat_type, user_id, user_name, thread_id=…, scope_id=…, message_id=…)`
   (`:4119-4156`; sets `source.profile` via runner routing). `SessionSource` fields: `gateway/session.py:64-98`.
   `scope_id` = guild/server id (dual-written alias `guild_id`); `thread_id` = thread/topic.
2. Create `MessageEvent` (`gateway/platforms/event.py:35-85`) and call `await self.handle_message(event)`
   (`:3489-3515`) — it spawns the background agent turn (`_start_session_processing`) and handles the
   busy-session queueing. Check `self._message_handler` is wired first (IRC pattern
   `plugins/platforms/irc/adapter.py:325-332`).
3. `handle_message` also calls `coerce_plaintext_gateway_command(event)` (`:1542,3496`) — so if a user
   types `/new` as plain text, it is recognized for any adapter (text fallback built into the base).

**Auth/fail-closed:** the runner installs an authorization check (`set_authorization_check` `:2208`);
adapters may also filter locally (IRC does, `adapter.py:318-320`). For Discord-like guild/role rules,
look at `plugins/platforms/discord/adapter.py:3929-4060` (slash auth mirrors message auth).
`enforces_own_access_policy` (`:1889-1897`) is only trusted for a real `allowlist` policy.

**Fatal errors / reconnect:** `_set_fatal_error(code, message, retryable=…)` `:2036-2040` +
`set_fatal_error_handler(...)` `:1994`; `_mark_connected/degraded/disconnected` `:2012-2034` write
runtime status. The gateway's fatal handler queues reconnect with backoff
(`gateway/run_adapters.py:200-236`); a non-retryable fatal removes the platform from the retry queue.
`_notify_fatal_error` is shielded against cancellation (`:2059-2084`) — call it from your reconnect
watchers. Adapters with streaming transports implement their own reconnect loop with backoff
(see `recovery.py` in discord).

**Platform identity lock (do this):** call
`self._acquire_platform_lock("<scope>", "<identity>", "<desc>")` (`:2086-2122`) in `connect()`;
it uses machine-global scoped locks (`$XDG_STATE_HOME/hermes/gateway-locks`, overridable via
`HERMES_GATEWAY_LOCK_DIR`, `gateway/status.py:169-175,1038-1083`) and fails with a descriptive
fatal error if another gateway (any profile) already holds the same bot identity. Pair with
`_release_platform_lock()` in `disconnect()` (`:2124-2131`). IRC example: `adapter.py:160-169,196-201`;
Discord: `plugins/platforms/discord/adapter.py:1230`.

**SendResult** (`:1559-1574`): `success: bool`, `message_id`, `error`, `raw_response`,
`retryable` (base auto-retries with `_send_with_retry` `:3152`), `retry_after`,
`continuation_message_ids`, `error_kind` (one of `SEND_ERROR_KINDS` `:1586-1587`).

**`_wire_plugin_handlers(native)`** (`:2133-2140`): call from `connect()` so plugins can attach
native handlers; IRC calls it with `None` (`adapter.py:193`).

---

## 6. Media: inbound attachments and outbound files

### 6.1 Inbound (platform → agent)

Your adapter downloads/caches attachments itself and hands the core **local file paths**:

- `MessageEvent.media_urls: list[str]` — local absolute paths; `media_types: list[str]` — MIME strings
  (`gateway/platforms/event.py:58-62`). `message_type` picks the class
  (`MessageType.VOICE/AUDIO/PHOTO/VIDEO/DOCUMENT/STICKER`, `event.py:15-25`). `media_text_inlined`
  exists for text/* inlining (`:61-62`).
- Cache helpers (in `gateway/platforms/base.py`): `cache_image_from_bytes(data, ext)` `:612`,
  `cache_audio_from_bytes` `:682`, `cache_video_from_bytes` `:712`,
  `cache_document_from_bytes(data, filename)` `:1408`, and the general dispatcher
  `cache_media_bytes(data, mime, …)` in `gateway/platforms/media_cache.py:69-97` (mime→ext tables
  `media_cache.py:18-37`). All return the cached path to put in `media_urls`.
- Size gate: `get_inbound_media_max_bytes()` reads `gateway.max_inbound_media_bytes`
  (live value 134217728; `0`/negative disables the cap) (`base.py:536-540`);
  `validate_inbound_media_size()` raises `ValueError` over the cap (`:543-549`); helpers for
  bounded streaming reads exist `:552-570`.
- STT is automatic for voice/audio: `_event_media_is_stt_input` (`gateway/run.py:2427-2432`) sends
  `MessageType.VOICE` or `media_type.startswith("audio/")` (but **not** AUDIO/DOCUMENT) into the shared
  transcription pipeline (`gateway/run_inbound.py:1954-1996` → `tools/transcription_tools.py:523,548`).
  So a voice clip just needs a correct `message_type`/`media_types` — no per-platform STT code.
- Discord reference for authenticated CDN downloads: `tests/gateway/test_discord_attachment_download.py`
  (docstring documents the three paths + SSRF-gated fallbacks).

### 6.2 Outbound (agent → platform)

`send()` carries **text only**. Files move through `MEDIA:<path>` tags in the model's reply text
(+ `[[audio_as_voice]]` / `[[as_document]]` modifiers): `BasePlatformAdapter.extract_media`
(`base.py:2836-2874`) strips them, the gateway validates each path
(`validate_media_delivery_path` `:1085-1127`, delivery roots/deny-list `:830-865`) and dispatches to
the adapter method by type: `_deliver_media_attachments` `:3714-3766` (image batch →
`send_multiple_images`; audio → `send_voice`; video → `send_video`; else `send_document`).
Wired together in `send_final_ledgered`/`_deliver_attachments` `:3782-3850`.

So to support outbound media, override: `send_image_file` and/or `send_image(url)`,
`send_voice`, `send_video`, `send_document` (+ optionally `send_multiple_images`). On any upload
failure return `SendResult(success=False, error=…)` — a failed attachment must never degrade to a
"successful" text notice (`:2742-2765`, and the Discord #66797 note in
`plugins/platforms/discord/adapter_media.py:48-61`). Discord's `DiscordMediaMixin`
(`adapter_media.py`, 347 lines) is the cleanest reference for a Discord-shaped platform: file
attachments, multi-image chunking (10/msg), native voice messages, forum-thread posting.

---

## 7. Slash commands: core mechanism vs. native registration

**Core mechanism is text-based and generic.** Commands are parsed from message text:
`MessageEvent.is_command()/get_command()` (`event.py:90-100`) → the runner resolves them against the
central command registry `hermes_cli.commands` (`resolve_command`, `is_gateway_known_command` —
used at `gateway/run_inbound.py:723-745`) → per-command handlers (`gateway/slash_commands.py`,
`gateway/run_inbound.py`). `/new` and `/reset` are the configured session-reset triggers
(`gateway/config.py:542`), handled at `gateway/run_inbound.py:771-777` with a destructive-action
confirm. `/update` is gated by `PlatformEntry.allow_update_command` for plugin platforms
(`slash_commands.py:1210-1220`). Platform-capability commands degrade to text when an adapter lacks
the capability (e.g. model picker falls back to a text card; `/voice` explains itself when there is
no voice channel support, `slash_commands.py:617-667`).

**Implication for fluxer:** if the client lets plain messages through, you get `/new`, `/stop`,
`/model` (text fallback) etc. for free once `send()`/`handle_message` work. If the Fluxer client
intercepts `/`, set `typed_command_prefix = "!"` (base `:1798`) or implement native registration.

**Native registration is bespoke per adapter, not generic.** Discord registers its own
`app_commands` tree (`plugins/platforms/discord/adapter.py:90-153` command list, `:1134`
`slash_commands` flag, `:1339-1340` registration, `:2716-2749` sync/reconcile, `:4241`
`_run_simple_slash`); its slash authorization mirrors message auth (`:3929-4060`). Tests:
`tests/gateway/test_discord_slash_commands.py`, `test_discord_slash_auth.py`,
`test_discord_thread_slash_expired_defer.py`. There is no `ctx.register_platform_command` —
reuse the Discord pattern only if Fluxer has a native command API. Plugin-defined commands
(`ctx.register_command`, §4) are bare-name `/name` in-session commands and work through the same
text dispatch — usable if fluxer needs custom commands.

---

## 8. Voice hooks

- **Text/voice notes (STT+TTS) are shared core**, not per-platform:
  - Inbound transcription: §6.1 (`run_inbound.py:1954-1996`; providers from `stt.provider`,
    `tools/transcription_*.py`).
  - Per-chat voice mode state: `$HERMES_HOME/gateway_voice_mode.json` (`gateway/run.py:3806`),
    toggled by `/voice [on|off|tts|status]` (`slash_commands.py:617-667`).
  - Outbound TTS: the base `prepare_tts_text`/`play_tts` → `send_voice` chain (`base.py:2671-2685`),
    plus optional streaming-TTS hooks (`:2687-2726`). Providers from `tts.provider`.
  - So fluxer "voice messages" (audio attachments) need only: inbound audio → `media_urls` +
    `MessageType.VOICE`; outbound → implement `send_voice` (e.g. upload as a voice-message file).
- **Live voice channels (join/leave/mix/speak) are Discord-bespoke**: `/voice channel|join|leave`
  handlers (`slash_commands.py:643-645`), `get_voice_channel_info`/`join_voice_channel` are optional
  adapter methods probed via hasattr (`:651-653`); implementation lives in
  `plugins/platforms/discord/voice_mixer.py` + the voice lane in `adapter.py`
  (tests: `test_discord_voice_mixer.py`, `test_discord_opus.py`). If Fluxer has RTC voice, plan for a
  separate effort; nothing generic exists to reuse beyond `gateway_voice_mode.json` semantics.

---

## 9. Config & secrets

### 9.1 `config.yaml` → `gateway.platforms.<name>`

`PlatformConfig` dataclass: `gateway/config.py:385-443`. Keys: `enabled`, `token`, `api_key`,
`home_channel` (→ `HomeChannel` `:290-309`), `reply_to_mode` ("off"/"first"/"all"), `typing_indicator`,
`typing_status_text`, `gateway_restart_notification`, `channel_overrides`, `extra` (free-form;
adapters read their keys there). A top-level `<name>:` block also works and wins over
`gateway.platforms.<name>` (`gateway/config_loader.py:171-180`; `_is_platform_name` filter `:163-168`).

Shared keys bridged into `extra` for **every** platform incl. plugin platforms
(`shared_loop_targets` `config_loader.py:239-246`; `_SHARED_KEYS` `:197-213`):
`unauthorized_dm_behavior`, `notice_delivery`, `reply_prefix`, `reply_in_thread`,
`cron_continuable_surface`, `require_mention`, `send_read_receipts`,
**`free_response_channels`**, `mention_patterns`, `exclusive_bot_mentions`, `dm_policy`, `allow_from`,
`allow_admin_from`, `user_allowed_commands`, `group_policy`, `group_allow_from`,
`group_allow_admin_from`, `group_user_allowed_commands`, `channel_prompts`,
`gateway_restart_notification`, `typing_indicator`, `typing_status_text`.
**Not bridged for plugin platforms:** `channel_skill_bindings` (Discord/Slack only, `:210`) — read it
from your own `extra`/`apply_yaml_config_fn` if fluxer wants channel→skill binding
(helper available: `resolve_channel_skills`/`resolve_channel_prompt`, `base.py:1716-1750`).

### 9.2 Env vs YAML precedence

`load_gateway_config()` = legacy JSON → YAML layer → `GatewayConfig.from_dict` →
`_apply_env_overrides` (`gateway/config.py:769-795`). Env overrides run **after** YAML, so env wins
where both exist. Your plugin's `apply_yaml_config_fn` may set `os.environ` during the YAML layer
(guard with `not os.getenv(...)` to preserve env>YAML, `ADDING_A_PLATFORM.md:24-31`).

### 9.3 Env enablement (no config.yaml entry needed)

At the end of `_apply_env_overrides`, `_enable_plugin_platforms_from_env` iterates every registered
plugin entry (`gateway/config_env.py:420-434`). For each:
`env_enablement_fn()` seeds `extra` (plus optional `home_channel` dict) → `is_connected(cfg)` gates
enablement → deps check → `platform_config.enabled = True` (`:375-417`). So with a good
`env_enablement_fn`, `FLUXER_BOT_TOKEN` (+ whatever minimum) in `.env` alone brings the platform up
and shows in `gateway status`. IRC example: `plugins/platforms/irc/adapter.py:426-446`.

### 9.4 Secrets

- Adapters read secrets through `get_scoped_secret(name, default)` from
  `gateway/platforms/_shared.py:17-30` (multiplex/profile-aware; falls back to `os.environ` on the
  default profile). Do not use `os.getenv` directly for tokens.
- `.env` loading: `$HERMES_HOME/.env` overrides shell exports (`hermes_cli/env_loader.py:311-352`);
  `$HERMES_HOME/.op.env` after. Live token exists as `FLUXER_BOT_TOKEN` in `/home/agent/.hermes/.env`.
- **Cron home channel**: register `cron_deliver_env_var="FLUXER_HOME_CHANNEL"`. Cron resolves it
  via `platform_registry` (`cron/scheduler_delivery.py:360-367,372+`) and mirrors it in the home-target
  map (`:39-53,474-476`). Without it, `deliver=fluxer` is "invalid".
- **Standalone sender** (cron/procs without a live gateway): `standalone_sender_fn` — the caller
  contract is in `tools/send_message_senders.py:311-326` and `tools/send_message_tool.py:449-482`
  (live adapter first, then standalone). IRC's `_standalone_send` (`adapter.py:537+`) is the template.
- Build a `PlatformEntry` once with immutable data; secrets are read at connect time so rotation via
  `.env` reload works per turn (`gateway/run.py:1589-1618` reloads .env each turn, config stays
  authoritative for budgets).

---

## 10. Session routing

- Session key: `build_session_key()` — `gateway/session.py:641-682`. Layout:
  `agent:<profile|main>:<platform>:<chat_type>[:<slack scope_id>][:<chat_id>][:<thread_id>][:<user>]`.
  - DMs: `chat_id` isolates by chat, falling back to sender id, then one session per platform;
    participant id goes **before** `thread_id` (`:679-681`).
  - Groups/channels: user isolation only when `group_sessions_per_user` (default True) and not in a
    thread; threads are shared unless `thread_sessions_per_user` (`:667-669`).
  - `chat_type` slot is whatever your source sets ("dm"/"group"/"channel"/"thread").
  - Discord precedent: `scope_id`(guild) is deliberately **not** part of the key (compat, `:647-648`);
    a channel-initiating message may key on `prospective_thread_id` so it matches later in-thread
    follow-ups (`:659-661`).
- What the adapter must provide: a correctly populated `SessionSource` via `build_source()` —
  `chat_id`, `chat_type` ("dm" vs else), `user_id`(+`user_name`), optional `thread_id`, `scope_id`,
  `message_id`, `parent_chat_id` for threads. That's all routing needs.
- Routing table lives per `HERMES_HOME` in `state.db` (`gateway_routing`; JSON mirror
  `sessions/sessions.json` when `gateway.write_sessions_json: true` — live config has it true).
  Sessions dir: `GatewayConfig.sessions_dir` = `$HERMES_HOME/sessions` (`config.py:544`).

---

## 11. Tests

**How they run:** pytest from the checkout; `pyproject.toml:582-595` sets `testpaths=["tests"]`,
`addopts = -m 'not integration'`. Plugin-platform tests live in `tests/plugins/platforms/<name>/`
(examples: `buzz/`, `photon/`) and gateway adapter tests in `tests/gateway/test_<platform>_*.py`.
Run the fluxer subset with:
`cd /home/agent/.hermes/hermes-agent && ./venv/bin/python -m pytest tests/plugins/platforms -k fluxer -q`
(and `tests/gateway -k fluxer`). Use a scratch `HERMES_HOME`/tmp dirs in tests; the state module
refuses to open the production `state.db` from test contexts (`hermes_state.py:185-208`).

**~12 most relevant existing files to mirror** (all under `tests/gateway/` unless noted):

| File | What it pins |
|---|---|
| `test_discord_connect.py` | connect path, deps probe, adapter construction. |
| `test_discord_send.py` | `send()` behavior/chunking/metadata. |
| `test_discord_attachment_download.py` | inbound attachment caching (3 paths + SSRF-gated fallbacks). |
| `test_discord_attachment_receipts.py` | inbound attachment bookkeeping. |
| `test_discord_media_metadata.py` | `send_voice/send_image_file/send_image` accept `metadata=` (parity check). |
| `test_discord_fail_closed_feedback.py` | fail-closed auth feedback + logging. |
| `test_discord_bot_auth_bypass.py` | allow-list vs bot-message gating (two gates). |
| `test_discord_slash_commands.py` / `test_discord_slash_auth.py` | native slash fast-paths + auth mirroring. |
| `test_discord_free_response.py` / `test_discord_allowed_channels.py` | channel gating (`free_response_channels` etc). |
| `test_discord_thread_persistence.py` | thread → session key continuity. |
| `test_discord_lazy_install_views.py` | check_fn/ensure_deps semantics (passive vs active). |
| `test_platform_plugin_handlers.py` (tests/gateway) | plugin `register_platform`/handler lifecycle via `discover_and_load(force=True)`. |
| `tests/plugins/platforms/photon/test_outbound_media.py` + `test_inbound.py` | a plugin-platform's own media tests. |
| `tests/plugins/platforms/buzz/test_buzz_unscoped_requirement_gate.py` | requirement-gate behavior for plugin platforms. |

Also useful: `tests/hermes_cli/test_plugins.py` (registry/gate lifecycle), and for the plugin's own
syntax/doctors: `hermes plugins doctor <path>` (`plugins_cmd.py:1981-1990` → `plugin_dev.py:282+`
runs the real scanner/registration) and `hermes plugins validate <path>` (CI gate,
`subcommands/plugins.py:50-53`).

---

## 12. Running a second gateway against a sandbox HERMES_HOME ✅ (verified)

### 12.1 Why it works / what is home-scoped vs machine-global

| State | Scope | Path / mechanism |
|---|---|---|
| gateway PID file | **per HERMES_HOME** | `$HERMES_HOME/gateway.pid` (`gateway/status.py:157-158`) |
| runtime lock (flock) | **per HERMES_HOME** | `$HERMES_HOME/gateway.lock` (`:161-162,659-687`) |
| control socket | **per HERMES_HOME** | `$HERMES_HOME/gateway.sock` (`gateway/control_socket.py:28-68`) |
| runtime status/state | **per HERMES_HOME** | `gateway_state.json` (`status.py:32,165-166`) |
| sessions / routing | **per HERMES_HOME** | `$HERMES_HOME/sessions`, `state.db` (`hermes_state.py:157`; `config.py:544`) |
| cron jobs | **per HERMES_HOME** | `$HERMES_HOME/cron` |
| starts ledger / storm breaker | **per HERMES_HOME** | `gateway-starts.log` (`status.py:57-84`) |
| **scoped token/bot locks** | **machine-global** | `$XDG_STATE_HOME/hermes/gateway-locks` (default `~/.local/state/hermes/gateway-locks`), override `HERMES_GATEWAY_LOCK_DIR` (`status.py:169-175,1038-1083`) |
| dashboard / port bindings | per process | no port is bound by a websocket-style platform; don't set `HERMES_DASHBOARD`; avoid port-binding platforms in the sandbox config (`gateway/config.py:274-287`). |

**Key consequence:** with a distinct `HERMES_HOME` the second gateway has its own PID/lock/state and
will refuse to start only if *that* home already has a running gateway
(`gateway/run.py:4816-4831`). The only *machine-global* coupling is a scoped bot lock — i.e. two
gateways cannot both connect the **same bot identity** even across homes (by design). Use a second
bot/token for the sandbox, or set `HERMES_GATEWAY_LOCK_DIR` to an isolated dir if you deliberately
want two processes on one identity (don't, for a live platform).

### 12.2 Step-by-step recipe (exact commands used and verified)

```bash
REPO=/home/agent/.hermes/hermes-agent
PY=$REPO/venv/bin/python
SANDBOX=/home/agent/workspace/fluxer/sandbox
HOME_S="$SANDBOX/hermes-home"

# 1) Fresh sandbox home (mimics $HERMES_HOME)
mkdir -p "$HOME_S"/{plugins,logs}
cat > "$HOME_S/config.yaml" <<'YAML'
gateway:
  platforms: {}         # enable fluxer only when you're ready:
  # platforms:
  #   fluxer:
  #     enabled: true
plugins:
  enabled: [fluxer-platform]   # needed only if the plugin lives under $HOME_S/plugins (user plugin)
YAML

# 2) (optional) minimal .env — do NOT copy the live .env wholesale.
#    Only what the sandbox needs; e.g.:
#      FLUXER_BOT_TOKEN=<sandbox bot token, NOT the live one>
printf 'FLUXER_BOT_TOKEN=...\n' > "$HOME_S/.env"     # only if you have a test token

# 3) (optional) plugin wiring for development, without touching the checkout:
#    a) user-plugin path (needs plugins.enabled above). Create the plugin dir anywhere you
#       like (e.g. /home/agent/workspace/fluxer/plugin-src) and symlink it in:
ln -s /home/agent/workspace/fluxer/plugin-src "$HOME_S/plugins/fluxer"   # dir containing plugin.yaml + __init__.py
#    b) or a bundled-style copy — but that edits the live install; prefer (a) + enabled list.

# 4) Start (background, clean env, logs to a file)
cd "$SANDBOX"
env -i HOME=/home/agent \
    PATH="$REPO/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 \
    HERMES_HOME="$HOME_S" \
    "$PY" "$REPO/hermes" gateway run -v > "$SANDBOX/gw.log" 2>&1 &
echo $!   # shell pid; the gateway pid is in $HOME_S/gateway.pid

# 5) Verify
HERMES_HOME="$HOME_S" "$PY" "$REPO/hermes" gateway status
tail -f "$SANDBOX/gw.log"     # expect: "Starting Hermes Gateway...", platform connect lines

# 6) Stop (clean, sandbox only)
HERMES_HOME="$HOME_S" "$PY" "$REPO/hermes" gateway stop
# or: kill -TERM "$(python -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$HOME_S/gateway.pid")"
```

Observed startup (✅ 2026-09-11, sandbox with no platforms):
`gateway.control_socket: Gateway control socket listening at …/sandbox/hermes-home/gateway.sock` →
`Starting Hermes Gateway...` → `No messaging platforms enabled.` → cron + housekeeping + kanban
dispatcher start → `Gateway will continue running for cron job execution.`
`hermes gateway status` under the sandbox home printed
`✓ Gateway is running (PID: 5710)  (Running manually, not as a system service)`;
`hermes gateway stop` printed `✓ Stopped gateway for this profile`, removed the sandbox PID file,
and left live PID 1 untouched.

### 12.3 Files to copy (subset, not all)

From `/home/agent/.hermes/` into `$HOME_S` only as needed:
`config.yaml` (trimmed: keep `model`/`web`/etc. only if you want identical agent behavior; **do not**
copy `gateway.platforms.discord` blocks if you don't want the sandbox on Discord),
`.env` **subset** (only the platform keys the sandbox needs; never the Discord token if the live
gateway is using it — the machine-global scoped lock would block the second connector, and worse,
`--replace` semantics could kill the live one; just don't).
Everything else (`state.db`, `sessions/`, `cron/`, caches, logs) is created fresh by the sandbox —
expected first-run side effects: `state.db` creation, `$HOME_S/bin/tirith` download, `skills/`
sync copy, `SOUL.md` seed (all observed inside the sandbox home ✅).

### 12.4 Conflicts & hazards checklist

- **Never** run `hermes gateway stop|restart|update` with the **default** `HERMES_HOME` in this
  container — PID 1 is the live gateway (killing it kills the container's main process).
- **Never** pass `--replace` to the sandbox gateway: with a shared machine-global scoped lock it can
  terminate the *live* holder cross-home (`gateway/platforms/base.py:2086-2122`;
  `gateway/status.py:1359-1386` `take_over_scoped_lock_holder`).
- Same-bot double connect: if your adapter takes a scoped platform lock (recommended; Discord,
  Telegram, Slack, IRC etc. do), the second gateway is refused with "already in use by … profile
  gateway"; if it doesn't, you'd get two live connectors on one bot — don't run the live token in the
  sandbox.
- Machine-global lock dir remains shared: `$XDG_STATE_HOME/hermes/gateway-locks`. Isolate only if you
  understand the consequence: `HERMES_GATEWAY_LOCK_DIR=$SANDBOX/locks`.
- Port checks: with only a websocket/polling platform (fluxer/discord-like) the sandbox binds **no
  TCP port**; the `api_server`, `webhook`, `msgraph_webhook`, `feishu`, `wecom_callback`,
  `bluebubbles`, `sms`, `whatsapp_cloud`, `line`, `teams` platforms are the port-binding set
  (`gateway/config.py:274-287`) — keep them out of the sandbox `config.yaml`.
- Don't set `HERMES_DASHBOARD` for the sandbox; the desktop/dashboard spawn their own processes and
  ports.
- Process-table sweeps: the CLI's orphan reaper/process scan excludes the entire ancestor chain
  (`hermes_cli/gateway.py:523-538,563-601`) plus service/recorded PIDs (`:1662-1702`), so in this
  container the live PID 1 is never a sweep candidate. Still treat every `hermes gateway …` command
  as home-sensitive: always prefix `HERMES_HOME=$HOME_S`.
- Ambient env: the agent's terminal layer strips `HERMES_SESSION_*` for its children, but pass a
  clean env anyway (`env -i …` above) so no session/profile markers leak into the second gateway.
- In-home singletons (only relevant if you *share* a home): kanban dispatcher lock
  (`$HERMES_HOME/kanban/.dispatcher.lock`), state.db maintenance locks, cron dir — all keyed to the
  home, so two different homes never contend. No cross-home conflicts observed ✅.

### 12.5 Which command to use in the sandbox

`hermes gateway run -v` (foreground; `-v`→INFO, `-vv`→DEBUG, `-q` quiet) — the only sensible mode in a
container. `start/install` are no-ops in containers by design (`hermes_cli/gateway.py:5928-5975`
prints "The container runtime is your service manager"), and `stop/restart` operate on the recorded
PID of the selected `HERMES_HOME`.

---

## 13. CLI cheat sheet

**Plugins** (`hermes_cli/subcommands/plugins.py:10-148`, `plugins_cmd.py`):
- `hermes plugins list [--user|--no-bundled|--plain|--json]` — registry keys (= manifest name for flat).
- `hermes plugins enable <name>` / `disable <name>` — edits `plugins.enabled/disabled`; takes effect
  next session/gateway restart (`plugins_cmd.py:1014`).
- `hermes plugins install <catalog-name | git-url | owner/repo[#subdir]> [--ref SHA] [--enable|--no-enable]`
  (`:18-39`, `:670-760`). **No plain local-path install** — the resolver only accepts catalog names,
  URLs (`https/http/file`), or `owner/repo` (`plugins_cmd.py:204-239`). Local dev = put the dir in a
  plugins directory (or symlink) instead.
- `hermes plugins update|remove|show|search|browse|capabilities`.
- `hermes plugins validate <dir>` — CI-style manifest checks (`plugin_validate.py:407+`).
- `hermes plugins doctor <path|id…>` — runs your plugin through the **real** discovery/registration
  scanner (`plugin_dev.py:282+`, `plugins_cmd.py:1981-1990`); use `--ci` to fail on errors.

**Gateway** (`hermes_cli/subcommands/gateway.py:28-171`):
- `hermes gateway run [-v|-q] [--replace] [--force] [--no-supervise] [--external-supervisor]`.
- `start|stop|restart [--all] [--system]` — service-managed installs; in a container they fall back
  to PID-file stop / in-process `run_gateway` (`gateway.py:6086-6175`).
- `status [--deep] [-l]`, `list`, `setup`, `install/uninstall`, `migrate-legacy`, `enroll`.
- Container semantics are coded: `is_container()` branches print "Service installation is not needed
  inside a Docker container" / recommend `hermes gateway run` (`gateway.py:5928-5975`, `:2455-2459`);
  s6-supervised image redirects `run` to the s6 service (`:5784-5830`) — not the case here (no s6).

### Other integration surfaces worth knowing

- **Channel directory (`hermes send` targets, cron delivery)**: `build_channel_directory()`
  (`gateway/channel_directory.py:133-170`) first asks each adapter for
  `await adapter.list_channels()` — if you implement it (returning `[{id/chat_id, name, type?}]`,
  normalized at `:136-141`), Fluxer channels show up in delivery-target lookups for free. Without it,
  plugin platforms get session-based discovery (`:153-163`, dynamic enum members are handled here).
  Discord's variant is `_build_discord` (`:173+`).
- **`platform_toolsets`**: per-platform tool pick via config `platform_toolsets.<name>`; a plugin
  platform falls back to composite `hermes-<platform>` (`hermes_cli/tools_config.py:175-177`), so set
  an explicit `platform_toolsets.fluxer: [...]` list in config.yaml (like the live config does for
  `discord: [hermes-discord]`) or define the composite in `toolsets.py` (built-in path only).
  Resolution logic: `_get_platform_tools` (`tools_config.py:551-602`).
- **`platform_registry` purpose recap**: registry entries are the single source the runner consults
  when creating adapters (`create_adapter` first, built-ins second — `gateway/run_adapters.py:1453`),
  plus status/setup/`send_message`/cron-delivery resolution (`tools/send_message_tool.py:449-482`,
  `cron/scheduler_delivery.py:360-367`). Registration is per-HERMES_HOME scope.
- **Session env for the adapter's own subprocesses**: `gateway/session_context.py:41` lists the
  `HERMES_SESSION_*` vars bound per platform session (chat id, platform, user); the agent prompt/
  system context uses them; nothing extra needed for a new platform beyond correct `SessionSource`.
- **`gateway.sock` control verbs** are few (`pause-for-update`; `gateway/run.py:5018-5068`,
  `gateway/control_socket.py`) — not an extension surface for plugins.

---

## 14. Top gotchas (rank-ordered)

1. **No hot reload** — plan for a gateway restart to pick up code/config changes (§2).
2. **In this container the restart of the live gateway = killing PID 1.** Develop against the
   sandbox gateway; leave the live restart to the operator (§2.4, §12).
3. User-dir plugins need `plugins.enabled`; bundled-dir plugins don't. Until then the platform name
   doesn't resolve and a `config.yaml` block for it is **silently dropped** (`config_loader.py:163-168`,
   `config.py:676-686`) — check `hermes plugins list` when nothing connects.
4. `__init__.py` exporting `register` is mandatory (`plugins_loader.py:431-434`).
5. `check_fn` must be passive; put installers in `ensure_deps_fn` (`platform_registry.py:52-64`).
   Getting this wrong has boot-looped the desktop app historically.
6. Adapter-created state is created once per gateway start (`run_startup.py:945-990`); reconnect
   watchers reuse the startup object.
7. Media in = local paths in `media_urls` + correct `media_types`; media out = `MEDIA:` tags mapped to
   your `send_*` overrides. A failed upload must not become a success (§6).
8. Scoped bot locks are machine-global — two gateways can't share one bot identity; `--replace` on
   the sandbox can kill the live holder (§12.4).
9. `channel_skill_bindings` is Discord/Slack-only in the shared-key bridge; `free_response_channels`
   is bridged for all platforms (§9.1).
10. `get_scoped_secret` (not `os.getenv`) for credentials; `env_enablement_fn` decides whether env-only
    setups appear in `gateway status` (§9.3-9.4).
11. `max_message_length=0` means "no advertised cap"; set it (and/or `splits_long_messages`) so the
    router chunks correctly (§4).
12. If the client eats `/`, set `typed_command_prefix` or the text command fallback silently
    disappears (§7).

---

## 15. Adapter implementation checklist (fluxer)

- [ ] `plugins/platforms/fluxer/__init__.py` → `from .adapter import register`
- [ ] `plugin.yaml`: `kind: platform`, `name: fluxer-platform` (or `fluxer`), `label`, env vars
- [ ] `register(ctx)` → `ctx.register_platform(name="fluxer", label=…, adapter_factory=FluxerAdapter, check_fn=…, validate_config=…, is_connected=…, required_env=[…], setup_fn=…, env_enablement_fn=…, cron_deliver_env_var="FLUXER_HOME_CHANNEL", standalone_sender_fn=…, allowed_users_env="FLUXER_ALLOWED_USERS", allow_all_env="FLUXER_ALLOW_ALL_USERS", max_message_length=…, emoji=…, pii_safe=False, allow_update_command=True, platform_hint=…)`
- [ ] Adapter `__init__`: `super().__init__(config=cfg, platform=Platform("fluxer"))`; read `cfg.extra` + `get_scoped_secret`
- [ ] `connect(is_reconnect=False) -> bool` (+ scoped platform lock; `_mark_connected`; start receive loop)
- [ ] `disconnect()` (+ release lock; `_mark_disconnected`)
- [ ] `send()` → `SendResult` (+ chunking honoring `max_message_length`)
- [ ] `send_typing` (if the platform has it); `get_chat_info`
- [ ] Inbound: build `SessionSource` (`build_source`) + `MessageEvent` + `handle_message(event)`; filter self-messages
- [ ] Media in: cache to local paths (`cache_media_bytes`/`cache_*_from_bytes`), set `media_urls/media_types/message_type`
- [ ] Media out: `send_image_file`/`send_image`/`send_voice`/`send_video`/`send_document` as supported
- [ ] Errors: `_set_fatal_error(..., retryable=)` + reconnect with backoff
- [ ] `platform_hint` text (formatting/style constraints for the model)
- [ ] Tests mirroring §11 + `hermes plugins doctor` + `hermes plugins validate`
- [ ] Sandbox-run verification (§12) with a test bot token

---

## 16. Open / verify next (explicitly UNSURE)

- **UNSURE (timing):** whether `Platform._missing_`'s cached bundled-name set could ever go stale within
  one process after adding a new bundled platform dir mid-run — it is cached at first lookup
  (`gateway/config.py:234-241`) and (per §2) the process would be restarted anyway. Not load-bearing.
- **UNSURE (minor):** `hermes plugins enable` key for a *bundled* platform: the loader matches both
  the canonical key and the manifest `name` (`plugins_discovery.py:179-180`), but bundled platforms
  never need enabling; only relevant if you deliberately put fluxer in `plugins.disabled`.
- **UNSURE (unexercised):** a sandbox gateway connecting to a *live* platform bot was not tested
  (deliberately: would either be refused by the scoped lock or, with `--replace`, risk the live
  gateway). The lock-refusal path is documented in `base.py:2086-2122` but treat as untested here.
- **UNSURE (env):** `HERMES_BUNDLED_PLUGINS` overrides the bundled plugins dir for discovery
  (`hermes_cli/plugins.py:67-73`) but the `Platform` enum's bundled scan uses the hardcoded
  `repo/plugins/platforms` path (`gateway/config.py:257`); if you test a bundled copy from another
  root, confirm the enum still resolves the name (registry fallback should cover it once
  `register()` runs).

