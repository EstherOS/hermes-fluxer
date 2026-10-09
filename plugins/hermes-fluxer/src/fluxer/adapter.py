"""Fluxer platform adapter for Hermes Agent — wave 1 (text) + wave 2 (attachments) + wave 3 (voice).

Contract: ``docs/spec-fluxer-plugin.md`` §2.4/§6-W2/§6-W3 (repo ``/home/agent/workspace/fluxer``).
Protocol: ``docs/fluxer-api-notes.md``. Conventions (``chat_type`` strings, two-gate
message filtering, chunking via ``truncate_message``) follow
``plugins/platforms/discord/adapter.py``; structure (``register()``, interactive setup,
standalone sender, env enablement) follows ``plugins/platforms/irc/adapter.py``;
media conventions (local paths in ``media_urls``, ``send_*`` overrides, a failed
upload never becomes a success) follow ``plugins/platforms/discord/adapter_media.py``
and the integration guide §6.

The client modules (``.rest``, ``.gatewayws``, ``.models``) are C1's frozen interfaces
(spec §2.1–2.3): ``FluxerREST`` / ``FluxerAPIError`` and ``FluxerGatewayClient``.
``.media`` (wave 2, this file's sibling) owns inbound attachment download/caching.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote, urlsplit

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, classify_send_error
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)

# ── backward-compatible omni import ───────────────────────────────────────────
# Prefer the migrated hermes_omni package; fall back to the local .omni modules
# so the adapter works both in a monorepo layout and a standalone installation.
_omni_imported = False
_omni_import_error: str | None = None
try:
    import omnimaker
    from omnimaker import resolve_profile, register_backend
    from omnimaker.backends.registry import get_backend, list_backends
    from omnimaker.types import SenseBinding, Part

    _omni_imported = True
except ImportError:
    try:
        from . import omni as _omni  # noqa: F401

        _omni_imported = True
    except ImportError as exc:
        _omni_import_error = str(exc)
if not _omni_imported:
    logger.debug(
        "Fluxer: omni engine not available (%s); text-only mode", _omni_import_error
    )

from .gatewayws import FluxerGatewayClient
from .media import VOICE_MESSAGE_FLAG, cache_inbound_attachments, download_attachment
from .models import AT_MENTION_RE, parse_message
from .rest import MAX_ATTACHMENT_BYTES, FluxerAPIError, FluxerREST
from .voice import try_livekit
from .voice.config import VoiceConfig, parse_voice_config
from .voice.controller import VoiceChannelRef, VoiceController
from .adapter_omni_mixin import OmniAdapterMixin

# Events routed to the voice controller (wave 3, spec §6-W3).
_VOICE_EVENT_TYPES = frozenset(
    {"VOICE_SERVER_UPDATE", "VOICE_STATE_UPDATE", "GUILD_CREATE"}
)

DEFAULT_API_BASE = "https://api.fluxer.app/v1"
DEFAULT_GATEWAY_URL = "wss://gateway.fluxer.app"
# Bots/webhooks take max(resolved, 4000) on create-message (api notes §2).
MAX_MESSAGE_LENGTH = 4000
# ``send_typing`` self-throttle; the base ``_keep_typing`` loop polls faster than this.
TYPING_THROTTLE_SECONDS = 8.0
_TRUTHY = {"1", "true", "yes", "on"}
# Channel types (api notes §2): 1 = DM, 3 = group DM; the rest of the wave-1 text
# scope (0 text, 2 guild voice, 5 guild link, 998 extended) is guild-shaped.
_DM_CHANNEL_TYPES = {1, 3}


# ── small helpers ────────────────────────────────────────────────────────────


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _int_or_none(value: Any) -> Optional[int]:
    """Best-effort int (Message payload ``flags``); ``None`` for missing/junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: Any) -> datetime.datetime:
    """ISO-8601 string (or datetime) → timezone-aware datetime; now(UTC) on junk."""
    if isinstance(raw, datetime.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=datetime.timezone.utc)
    if isinstance(raw, str) and raw:
        text = raw.strip().replace("Z", "+00:00")
        with contextlib.suppress(ValueError):
            parsed = datetime.datetime.fromisoformat(text)
            return (
                parsed
                if parsed.tzinfo
                else parsed.replace(tzinfo=datetime.timezone.utc)
            )
    return datetime.datetime.now(datetime.timezone.utc)


def _csv_set(raw: Any) -> Set[str]:
    """Comma/semicolon-separated env value (or list) → cleaned id set."""
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {
        part.strip() for part in str(raw).replace(";", ",").split(",") if part.strip()
    }


def _is_wellformed_token(token: Any) -> bool:
    """``<application_id>.<secret>`` shape (spec §2.4: contains '.')."""
    text = str(token or "").strip()
    return (
        bool(text)
        and "." in text
        and not text.startswith(".")
        and not text.endswith(".")
    )


# ── adapter ──────────────────────────────────────────────────────────────────


class FluxerAdapter(BasePlatformAdapter, OmniAdapterMixin):
    """Fluxer bot adapter — guild channels + DMs; text + attachments (waves 1–2)."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    MAX_ATTACHMENTS_PER_MESSAGE = (
        10  # Fluxer attachment-request cap per message (api notes §3)
    )
    splits_long_messages = (
        True  # send() chunks via truncate_message(MAX_MESSAGE_LENGTH)
    )
    supports_code_blocks = True  # Fluxer renders Discord-style fenced code blocks

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("fluxer"))
        self._extra: dict = getattr(config, "extra", {}) or {}
        self._free_response_channels: Set[str] = {
            str(c) for c in (self._extra.get("free_response_channels") or [])
        }
        self._ignore_bots: bool = _as_bool(self._extra.get("ignore_bots"), default=True)
        # Guild messages need a mention unless the channel is free-response; an explicit
        # ``require_mention: false`` opts into responding to every guild message.
        self._require_mention: bool = _as_bool(
            self._extra.get("require_mention"), default=True
        )
        self._mention_patterns: List[re.Pattern] = self._compile_patterns(
            self._extra.get("mention_patterns")
        )
        # Env wins over extra, mirroring the IRC adapter's precedence.
        self._api_base: str = str(
            _get_scoped_secret("FLUXER_API_BASE")
            or self._extra.get("api_base")
            or DEFAULT_API_BASE
        )
        self._gateway_url: str = str(
            _get_scoped_secret("FLUXER_GATEWAY_URL")
            or self._extra.get("gateway_url")
            or DEFAULT_GATEWAY_URL
        )
        self._rest: Optional[FluxerREST] = None
        self._ws: Optional[FluxerGatewayClient] = None
        self._bot_id: Optional[str] = None
        self._bot_name: Optional[str] = None
        self._connected_once: bool = (
            False  # suppress WS lifecycle noise during initial connect
        )
        self._channel_names: Dict[str, str] = {}  # lazily cached chat names
        self._last_typing: Dict[str, float] = {}  # send_typing self-throttle
        self._unauthorized_logged: Set[str] = set()
        # ── voice lane (wave 3, spec §6-W3) ──────────────────────────────
        # Config parses eagerly (cheap, total); the controller is built lazily and
        # stays inert when voice.enabled=false or livekit is missing.
        self._voice_cfg: VoiceConfig = parse_voice_config(
            self._extra.get("voice"), fallback_guild=None
        )
        self._voice: Optional[VoiceController] = None
        self._voice_start_task: Optional["asyncio.Task"] = None
        # Core /voice integration state — names/signatures mirror the discord adapter
        # so gateway.run_voice's hasattr-probes find them (see slash_commands.py:617+).
        self._voice_text_channels: Dict[int, str] = {}
        self._voice_sources: Dict[int, dict] = {}
        self._voice_input_callback = None
        self._last_command_source: Optional[dict] = None
        # ── hermes-omni engine (replaces old voice dispatch when configured) ─
        self._omni_cfg: dict | None = None
        self._omni_session: Any | None = None
        # Load the omni section from the root config file — it's at YAML root,
        # not inside the platform config subtree passed to __init__.
        try:
            import os, yaml
            config_path = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
            if os.path.isdir(config_path):
                config_file = os.path.join(config_path, "config.yaml")
                if os.path.isfile(config_file):
                    with open(config_file) as f:
                        full_cfg = yaml.safe_load(f) or {}
                    self._omni_cfg = full_cfg.get("omni") or None
        except Exception:
            pass
        if self._omni_cfg and _omni_imported:
            try:
                self._init_omni_backends(self._omni_cfg)
                logger.info("Fluxer: hermes-omni engine initialised from config")
            except Exception as exc:
                logger.warning(
                    "Fluxer: hermes-omni init failed (%s); falling back to legacy voice",
                    exc,
                )

    # ── identity ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Fluxer"

    @staticmethod
    def _compile_patterns(raw: Any) -> List[re.Pattern]:
        patterns: List[re.Pattern] = []
        for entry in raw or []:
            try:
                patterns.append(re.compile(str(entry)))
            except re.error as e:  # a bad operator regex must not break the adapter
                logger.warning(
                    "Fluxer: ignoring invalid mention pattern %r: %s", entry, e
                )
        return patterns

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    # ── voice lane plumbing (wave 3, spec §6-W3) ─────────────────────────

    def _voice_controller(self) -> Optional[VoiceController]:
        """Lazily build the voice controller; None when disabled or livekit missing.

        Construction imports no livekit (the controller imports it inside its
        functions) — the probe here keeps a fresh venv text-only without even
        building the object.
        """
        if not self._voice_cfg.enabled:
            return None
        if self._voice is None:
            if try_livekit() is None:
                return None
            self._voice = VoiceController(self, self._voice_cfg)
        return self._voice

    def _voice_start_after_connect(self) -> None:
        """Seed voice state from READY and kick off ``voice.auto_channels``."""
        if not self._voice_cfg.enabled:
            return
        ctl = self._voice_controller()
        if ctl is None:
            return
        ready = getattr(self._ws, "ready_payload", None)
        if ready:
            ctl.seed_from_ready(ready)
        if not self._voice_cfg.auto_channels or self._voice_start_task is not None:
            return
        try:
            self._voice_start_task = asyncio.get_running_loop().create_task(
                self._voice_autostart(ctl)
            )
        except RuntimeError:  # pragma: no cover - connect() always runs in a loop
            logger.debug("Fluxer: voice auto_channels skipped (no running event loop)")

    async def _voice_autostart(self, ctl: VoiceController) -> None:
        try:
            await ctl.start_auto_channels()
        except Exception:
            logger.exception("Fluxer: voice auto_channels failed")

    def _route_voice_event(self, event_type: str, data: dict) -> None:
        """VOICE_* / GUILD_CREATE → controller (errors isolated from the WS loop)."""
        if not self._voice_cfg.enabled:
            return
        try:
            ctl = (
                self._voice
                if self._voice is not None
                else (
                    self._voice_controller() if event_type != "GUILD_CREATE" else None
                )
            )
            if ctl is not None:
                ctl.on_gateway_event(event_type, data or {})
        except Exception:  # one bad event must never kill the dispatch loop
            logger.exception("Fluxer: voice event routing failed for %s", event_type)

    # ── connect / disconnect ─────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """REST identity → platform lock → gateway WS → handlers → connected.

        The lock comes AFTER ``/users/@me`` because the bot id is the lock identity
        (spec §2.4). Token is read here (connect time) so ``.env`` rotation applies.
        """
        token = str(_get_scoped_secret("FLUXER_BOT_TOKEN") or "").strip()
        if not token:
            return self._fail(
                "token_missing", "FLUXER_BOT_TOKEN is not set", retryable=False
            )
        rest = FluxerREST(token, base_url=self._api_base)
        try:
            me = await rest.get_me()
            if not isinstance(me, dict) or not me.get("id"):
                raise ValueError(f"unexpected /users/@me payload: {me!r}")
        except FluxerAPIError as e:
            with contextlib.suppress(Exception):
                await rest.close()
            status = getattr(e, "status", None)
            if status in (401, 403):
                return self._fail(
                    "auth_failed",
                    f"Fluxer auth rejected GET /users/@me: {e}",
                    retryable=False,
                )
            return self._fail(
                "identity_fetch_failed", f"GET /users/@me failed: {e}", retryable=True
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                await rest.close()
            return self._fail(
                "identity_fetch_failed", f"GET /users/@me failed: {e}", retryable=True
            )
        self._bot_id = str(me.get("id"))
        self._bot_name = str(me.get("username") or "")
        if not self._acquire_platform_lock(
            "fluxer", self._bot_id, f"Fluxer bot {self._bot_id}"
        ):
            with contextlib.suppress(Exception):
                await rest.close()
            return False  # _acquire_platform_lock published the fatal error itself
        self._rest = rest
        try:
            self._ws = FluxerGatewayClient(
                token,
                url=self._gateway_url,
                on_event=self._on_gateway_event,
                on_connection_event=self._on_connection_event,
            )
            await self._ws.start()  # returns after READY
        except Exception as e:
            logger.error("Fluxer: gateway connect failed: %s", e)
            await self._teardown_transport()
            return self._fail(
                "connect_failed",
                f"Fluxer gateway connect failed: {e}",
                retryable=bool(getattr(e, "retryable", True)),
            )
        if self._ws.user_id and str(self._ws.user_id) != self._bot_id:
            logger.warning(
                "Fluxer: gateway READY user %s does not match REST identity %s",
                self._ws.user_id,
                self._bot_id,
            )
        self._wire_plugin_handlers(None)
        self._connected_once = True
        self._mark_connected()
        self._voice_start_after_connect()
        logger.info(
            "Fluxer: connected as %s (%s) — REST %s, gateway %s",
            self._bot_name or self._bot_id,
            self._bot_id,
            self._api_base,
            self._gateway_url,
        )
        return True

    async def disconnect(self) -> None:
        """Stop WS, close REST, release the identity lock, publish disconnected (spec §2.4)."""
        await self._teardown_transport()
        self._mark_disconnected()

    async def _teardown_transport(self) -> None:
        # Leave voice before the socket dies (op4 null needs the live WS); bounded.
        if self._voice is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._voice.shutdown(), 15)
        if self._voice_start_task is not None:
            self._voice_start_task.cancel()
            self._voice_start_task = None
        ws, rest = self._ws, self._rest
        self._ws = self._rest = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.stop()
        if rest is not None:
            with contextlib.suppress(Exception):
                await rest.close()
        self._release_platform_lock()
        self._last_typing.clear()

    # ── gateway events ───────────────────────────────────────────────────

    async def _on_gateway_event(self, event_type: str, data: dict) -> None:
        """WS dispatch router — wave 1 handles MESSAGE_CREATE; wave 3 routes voice."""
        if event_type == "MESSAGE_CREATE":
            try:
                await self._handle_message_create(data or {})
            except Exception:  # one bad event must never kill the WS dispatch loop
                logger.exception("Fluxer: error handling MESSAGE_CREATE")
        elif event_type in _VOICE_EVENT_TYPES:
            self._route_voice_event(event_type, data or {})
        else:
            logger.debug("Fluxer: unhandled gateway event %s", event_type)

    async def _on_connection_event(self, kind: str, payload: Optional[dict]) -> None:
        """Lifecycle events from FluxerGatewayClient (spec §2.2/§2.4)."""
        if kind == "ready":
            user = (payload or {}).get("user") or {}
            if user.get("id") and not self._bot_id:
                self._bot_id = str(user.get("id"))
            self._mark_connected()
            if self._voice is not None:
                self._voice.seed_from_ready(payload)
            logger.info("Fluxer: gateway ready (%s)", self._bot_id or "unknown bot id")
        elif kind == "resumed":
            self._mark_connected()
            logger.info("Fluxer: gateway session resumed")
        elif kind == "disconnected":
            info = payload or {}
            if not self._connected_once:
                # Initial connect() is still awaiting; it surfaces the failure itself.
                logger.debug("Fluxer: gateway dropped during initial connect: %s", info)
                return
            logger.warning(
                "Fluxer: gateway disconnected (code=%s reason=%s); reconnecting with backoff",
                info.get("code"),
                info.get("reason") or "",
            )
            self._mark_degraded()
        elif kind == "reconnect_failed":
            detail = payload or {}
            if not self._connected_once:
                logger.debug(
                    "Fluxer: gateway reconnect failed during initial connect: %s",
                    detail,
                )
                return  # connect() raises with the same information
            logger.error(
                "Fluxer: gateway reconnect failed: %s", detail or "client gave up"
            )
            self._set_fatal_error(
                "gateway_reconnect_failed",
                f"Fluxer gateway reconnect failed: {detail or 'client gave up'}",
                retryable=True,
            )
            await self._notify_fatal_error()
        else:
            logger.debug("Fluxer: connection event %s: %s", kind, payload)

    # ── inbound: MESSAGE_CREATE filter chain ─────────────────────────────

    async def _handle_message_create(self, data: dict) -> None:
        """Filter chain (spec §2.4): self → bot flag → authorization → trigger → dispatch."""
        msg = parse_message(data)
        author = msg.get("author") or {}
        author_id = str(author.get("id") or "")
        username = str(
            author.get("display_name") or author.get("username") or author_id
        )
        chat_id = str(msg.get("channel_id") or "")
        if not author_id or not chat_id:
            logger.debug(
                "Fluxer: dropping malformed MESSAGE_CREATE (no author/channel)"
            )
            return
        label = username

        # 1. our own messages (every reply we send echoes back as MESSAGE_CREATE)
        if self._bot_id and author_id == self._bot_id:
            logger.info("Fluxer: ignored message from %s (self)", label)
            return
        # 2. other bots (``ignore_bots`` default True); controlled webhooks only when flagged bot
        if author.get("bot") and self._ignore_bots:
            logger.info(
                "Fluxer: ignored message from %s (bot author, ignore_bots=true)", label
            )
            return
        # 3. author authorization — local mirror of the runner's allowlist, fail closed
        if not self._is_user_authorized(author_id):
            if author_id not in self._unauthorized_logged:
                self._unauthorized_logged.add(author_id)
                logger.warning(
                    "Fluxer: ignored message from %s (unauthorized user %s; set FLUXER_ALLOWED_USERS "
                    "or FLUXER_ALLOW_ALL_USERS to allow)",
                    label,
                    author_id,
                )
            return
        content = str(msg.get("content") or "")
        channel_type = msg.get("channel_type")
        guild_id = msg.get("guild_id")
        is_dm = channel_type in _DM_CHANNEL_TYPES

        # 4. trigger policy
        why = self._trigger_reason(chat_id, content, is_dm=is_dm)
        if why is None:
            logger.info(
                "Fluxer: ignored message from %s (no trigger: mention required in %s)",
                label,
                chat_id,
            )
            return

        # 5. strip the bot mention, cache attachments (wave 2), build source + event, dispatch
        text = self._strip_bot_mention(content)
        attachment_data = msg.get("attachments") or []
        media_urls: List[str] = []
        media_types: List[str] = []
        message_type = MessageType.TEXT
        if attachment_data:
            try:
                (
                    media_urls,
                    media_types,
                    detected_type,
                ) = await cache_inbound_attachments(
                    attachment_data, flags=_int_or_none(msg.get("flags")) or 0
                )
            except Exception as e:  # media failure must still deliver the text
                logger.warning(
                    "Fluxer: attachment caching failed for message %s in %s: %s",
                    msg.get("id"),
                    chat_id,
                    e,
                )
            else:
                if detected_type is not None:
                    message_type = detected_type
                if media_urls:
                    logger.info(
                        "Fluxer: cached %d attachment(s) for message %s in %s (%s)",
                        len(media_urls),
                        msg.get("id"),
                        chat_id,
                        ", ".join(media_types),
                    )
        if not text and not media_urls:
            if attachment_data:
                text = "(the user sent an attachment that could not be retrieved)"
            else:
                logger.info(
                    "Fluxer: ignored message from %s (empty after mention strip)", label
                )
                return
        chat_name = await self._chat_name(chat_id)
        chat_type = (
            "dm" if is_dm else "group"
        )  # discord convention for guild text channels
        message_id = str(msg.get("id") or "") or None
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=author_id,
            user_name=username,
            scope_id=str(guild_id) if guild_id else None,
            guild_id=str(guild_id) if guild_id else None,
            message_id=message_id,
            is_bot=bool(author.get("bot")),
        )
        # Voice lane: remember the /voice command source so `session_binding: invoke`
        # can bind to the channel the command came from; expose guild context via
        # raw_message so core helpers (run_voice._get_guild_id) work on Fluxer too.
        if content.lstrip().startswith("/voice"):
            self._last_command_source = source.to_dict()
            # Intercept DM /voice join/leave locally — the core handlers need a
            # guild context that DMs lack (see ``_handle_dm_voice_command``).
            if is_dm:
                handled = await self._handle_dm_voice_command(
                    content=content, chat_id=chat_id, author_id=author_id, source=source
                )
                if handled:
                    return
        raw_guild = str(guild_id) if guild_id else None
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=message_id,
            timestamp=msg.get("timestamp") or _parse_timestamp(None),
            media_urls=media_urls,
            media_types=media_types,
            raw_message=(
                SimpleNamespace(guild_id=raw_guild, guild=None) if raw_guild else None
            ),
        )
        # Voice-bound channel ephemeral preamble: every turn in a voice-active chat
        # gets the ``voice.input_prompt`` context (reply will be spoken).
        vp = self._voice_input_prompt_for_chat(chat_id)
        if vp is not None:
            event.channel_prompt = vp
        logger.info("Fluxer: message from %s in %s (trigger=%s)", label, chat_name, why)
        if self._message_handler is None:
            logger.debug("Fluxer: message handler not wired; dropping event")
            return
        await self.handle_message(event)

    def _trigger_reason(
        self, chat_id: str, content: str, *, is_dm: bool
    ) -> Optional[str]:
        """Why this message should reach the agent, or None to drop it."""
        if is_dm:
            return "dm"  # DMs always trigger for authorized users
        if self._is_free_response(chat_id):
            return "free_response"
        stripped = self._strip_bot_mention(content)
        if content.lstrip().startswith("/") or stripped.startswith("/"):
            return "command"
        if self._mentions_bot(content):
            return "mention"
        if any(pattern.search(content) for pattern in self._mention_patterns):
            return "mention_pattern"
        if not self._require_mention:
            return "require_mention_disabled"
        return None

    def _is_free_response(self, chat_id: str) -> bool:
        return (
            "*" in self._free_response_channels
            or chat_id in self._free_response_channels
        )

    def _mentions_bot(self, content: str) -> bool:
        """``<@id>`` / ``<@!id>`` for our bot id (models.AT_MENTION_RE + fast substring)."""
        if not self._bot_id:
            return False
        if f"<@{self._bot_id}>" in content or f"<@!{self._bot_id}>" in content:
            return True
        with contextlib.suppress(Exception):
            return self._bot_id in AT_MENTION_RE.findall(content)
        return False

    def _strip_bot_mention(self, content: str) -> str:
        """Remove our own mention(s); content otherwise intact. Collapses only the
        whitespace runs the removal creates (never newlines)."""
        if not self._bot_id or not content:
            return content or ""
        pattern = re.compile(rf"<@!?{re.escape(self._bot_id)}>")
        text = pattern.sub(" ", content)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    async def _chat_name(self, chat_id: str) -> str:
        """Channel name, cached lazily from REST; falls back to the id."""
        if chat_id in self._channel_names:
            return self._channel_names[chat_id]
        name = ""
        rest = self._rest
        if rest is not None:
            try:
                channel = await rest.get_channel(chat_id)
                if isinstance(channel, dict):
                    name = str(channel.get("name") or "")
            except Exception as e:
                logger.debug(
                    "Fluxer: channel name lookup failed for %s: %s", chat_id, e
                )
        name = name or chat_id
        self._channel_names[chat_id] = name
        return name

    # ── inbound authorization (local mirror, fail closed) ────────────────

    def _allow_all_users(self) -> bool:
        if _as_bool(_get_scoped_secret("FLUXER_ALLOW_ALL_USERS"), default=False):
            return True
        return _as_bool(_get_scoped_secret("GATEWAY_ALLOW_ALL_USERS"), default=False)

    def _allowed_user_ids(self) -> Set[str]:
        allowed = _csv_set(_get_scoped_secret("FLUXER_ALLOWED_USERS"))
        # Parity with the runner's adapter-extra allowlists (shared bridged keys).
        for key in ("allow_from", "allowed_users"):
            allowed |= _csv_set(self._extra.get(key))
        return allowed

    def _is_user_authorized(self, user_id: str) -> bool:
        """FLUXER_ALLOW_ALL_USERS / FLUXER_ALLOWED_USERS (+extra allow lists); fail closed."""
        if self._allow_all_users():
            return True
        allowed = self._allowed_user_ids()
        if not allowed:
            return False
        return "*" in allowed or str(user_id) in allowed

    # ── outbound ─────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Chunked text send; voice-bound chats route through the voice hook first.

        When a voice session is bound to ``chat_id`` the reply is spoken into the
        voice channel via piper (``voice.transcripts == "channel"`` additionally
        posts the text; ``"off"`` suppresses the text post) — see
        :meth:`_voice_bound_send`.
        """
        session = None
        if self._voice is not None and self._voice_cfg.enabled:
            session = self._voice.session_for_chat(chat_id)
        if session is not None:
            return await self._voice_bound_send(
                session, chat_id, content, reply_to, metadata
            )
        return await self._send_text_chunked(chat_id, content, reply_to, metadata)

    async def _voice_bound_send(
        self,
        session,
        chat_id: str,
        content: str,
        reply_to: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> SendResult:
        """``send()`` for a chat owned by a voice session (wave 3, spec §6-W3).

        ``transcripts: channel`` posts the text and speaks it; ``off`` speaks
        only.  A piper failure with transcripts off falls back to a text post so
        a reply is never silently eaten (and a failed post never blocks speech).

        System/interim metadata (``_interim_send`` / ``non_conversational``) is
        posted as text but not spoken aloud — progress heartbeats, busy acks
        and approval prompts are read-only text, not part of the spoken
        conversation.
        """
        posted: Optional[SendResult] = None
        # System/interim sends (progress, long-running heartbeats, approval
        # prompts) are always posted as text but NOT spoken aloud.
        md = metadata or {}
        system_send = bool(md.get("_interim_send") or md.get("non_conversational"))
        if self._voice.config.transcripts == "channel":
            posted = await self._send_text_chunked(chat_id, content, reply_to, metadata)
            if not posted.success:
                logger.warning(
                    "Fluxer voice: transcript-mode post failed in %s: %s",
                    chat_id,
                    posted.error,
                )
        if system_send:
            # System/interim text is posted (above for channel mode); for off
            # mode the text must not be silently eaten — post explicitly.
            if posted is None:
                posted = await self._send_text_chunked(
                    chat_id, content, reply_to, metadata
                )
            logger.info("Fluxer voice: system/interim send not spoken in %s", chat_id)
            if posted.success:
                return posted
            # Fall through to speak if the explicit post also failed
        spoken = False
        auto_tts_active = self._should_auto_tts_for_chat(chat_id)
        if auto_tts_active:
            # Core auto-TTS (`_play_tts_file` → our `play_tts`) speaks this turn with
            # the configured Hermes TTS engine; a second piper copy would double-talk.
            logger.info(
                "Fluxer voice: auto-TTS active for %s; adapter piper speak skipped",
                chat_id,
            )
            spoken = True
        else:
            try:
                spoken = await self._voice.speak(
                    content, session=session, tag="send-hook"
                )
            except Exception as e:
                logger.warning("Fluxer voice: speak failed in %s: %s", chat_id, e)
        if posted is not None:
            return posted
        if spoken:
            return SendResult(
                success=True, message_id=None, raw_response={"voice_only": True}
            )
        logger.warning(
            "Fluxer voice: nothing spoken for %s; falling back to text post", chat_id
        )
        return await self._send_text_chunked(chat_id, content, reply_to, metadata)

    async def _send_text_chunked(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Chunked text send; ``reply_to`` → ``message_reference`` on the first chunk."""
        rest = self._rest
        if rest is None:
            return SendResult(
                success=False,
                error="Fluxer: not connected",
                retryable=True,
                error_kind="transient",
            )
        if not (content or "").strip():
            return SendResult(
                success=False, error="Fluxer: refusing to send empty message"
            )
        chunks = self.truncate_message(content, self.MAX_MESSAGE_LENGTH) or [content]
        reference = self._reply_reference(reply_to, metadata)
        message_ids: List[str] = []
        for i, chunk in enumerate(chunks):
            try:
                response = await rest.create_message(
                    chat_id,
                    content=chunk,
                    message_reference=(reference if i == 0 else None),
                )
            except FluxerAPIError as e:
                return self._send_error(e, message_ids)
            except Exception as e:
                logger.warning("Fluxer: send to %s failed: %s", chat_id, e)
                return SendResult(
                    success=False,
                    error=f"Fluxer send failed: {e}",
                    retryable=True,
                    error_kind=classify_send_error(e, str(e)),
                    message_id=(message_ids[-1] if message_ids else None),
                    continuation_message_ids=tuple(message_ids),
                )
            mid = str((response or {}).get("id") or "")
            if mid:
                message_ids.append(mid)
            if len(chunks) > 1:
                # create-message bucket is 20/10 s per channel — stay well under it.
                await asyncio.sleep(0.25)
        return SendResult(
            success=True,
            message_id=(message_ids[-1] if message_ids else None),
            continuation_message_ids=tuple(message_ids),
        )

    @staticmethod
    def _reply_reference(
        reply_to: Optional[str], metadata: Optional[Dict[str, Any]]
    ) -> Optional[dict]:
        """Reply reference from the explicit ``reply_to`` id only (no guessing / no fetching);
        ``channel_id``/``guild_id`` ride along only when metadata provides them."""
        if not reply_to:
            return None
        reference: Dict[str, Any] = {"message_id": str(reply_to)}
        if isinstance(metadata, dict):
            for key in ("channel_id", "guild_id"):
                if metadata.get(key):
                    reference[key] = str(metadata[key])
        return reference

    def _send_error(self, exc: FluxerAPIError, sent_ids: List[str]) -> SendResult:
        """Map a FluxerAPIError to SendResult: 5xx/429-exhausted retryable, 4xx not."""
        status = getattr(exc, "status", None)
        retryable = (
            True if not isinstance(status, int) else (status == 429 or status >= 500)
        )
        if status == 429:
            kind = "rate_limited"
        elif retryable:
            kind = "transient"
        else:
            kind = classify_send_error(
                exc, str(getattr(exc, "message", "") or str(exc))
            )
        detail = getattr(exc, "message", None) or str(exc)
        code = getattr(exc, "code", None)
        return SendResult(
            success=False,
            error=f"Fluxer send failed ({status if status is not None else '?'}"
            f"{' ' + str(code) if code else ''}): {detail}",
            retryable=retryable,
            retry_after=getattr(exc, "retry_after", None),
            error_kind=kind,
            message_id=(sent_ids[-1] if sent_ids else None),
            continuation_message_ids=tuple(sent_ids),
        )

    # ── outbound: media (wave 2, spec §6-W2) ─────────────────────────────

    async def _send_media(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        *,
        metadata: Optional[Dict[str, Any]] = None,
        reply_to: Optional[str] = None,
        flags: Optional[int] = None,
        file_name: Optional[str] = None,
        kind: str = "file",
    ) -> SendResult:
        """Upload one local file → ``create_message(attachments=[claim])``.

        The first caption chunk rides the media message; remaining chunks follow
        as plain ``send()`` calls (never truncated silently).  ``flags`` (e.g.
        ``VOICE_MESSAGE`` 8192) is attempted once — a 400 from create-message
        drops the flag and retries without it (live-verified: Fluxer rejects the
        flag with ``VOICE_MESSAGES_CANNOT_HAVE_CONTENT`` when a caption rides
        along, and ``VOICE_MESSAGES_ATTACHMENT_WAVEFORM_REQUIRED`` when the claim
        lacks ``waveform`` data).  Any failure is
        ``SendResult(success=False, …)``: a failed upload never degrades into a
        text-only "success" (integration §6.2, Discord #66797).
        """
        rest = self._rest
        if rest is None:
            return SendResult(
                success=False,
                error="Fluxer: not connected",
                retryable=True,
                error_kind="transient",
            )
        path = Path(str(file_path))
        if not path.is_file():
            return SendResult(
                success=False, error=f"Fluxer {kind} file not found: {file_path}"
            )
        chunks = [
            chunk
            for chunk in (
                self.truncate_message(caption or "", self.MAX_MESSAGE_LENGTH) or []
            )
            if chunk.strip()
        ]
        try:
            claim = await rest.upload_attachment(chat_id, str(path), filename=file_name)
        except FluxerAPIError as e:
            return self._send_error(e, [])
        except Exception as e:
            logger.warning("Fluxer: upload of %s failed: %s", path.name, e)
            return SendResult(
                success=False,
                error=f"Fluxer upload failed ({path.name}): {e}",
                retryable=True,
                error_kind=classify_send_error(e, str(e)),
            )
        attempt_flags = flags
        while True:
            try:
                response = await rest.create_message(
                    chat_id,
                    content=(chunks[0] if chunks else None),
                    attachments=[claim],
                    message_reference=self._reply_reference(reply_to, metadata),
                    flags=attempt_flags,
                )
                break
            except FluxerAPIError as e:
                if attempt_flags is not None and getattr(e, "status", None) == 400:
                    logger.info(
                        "Fluxer: create_message rejected flags=%s for %s (%s); retrying "
                        "without the flag",
                        attempt_flags,
                        path.name,
                        e,
                    )
                    attempt_flags = None
                    continue
                return self._send_error(e, [])
            except Exception as e:
                logger.warning(
                    "Fluxer: media message creation failed for %s: %s", path.name, e
                )
                return SendResult(
                    success=False,
                    error=f"Fluxer send failed: {e}",
                    retryable=True,
                    error_kind=classify_send_error(e, str(e)),
                )
        message_id = str((response or {}).get("id") or "") or None
        message_ids: List[str] = [message_id] if message_id else []
        for chunk in chunks[1:]:
            # create-message bucket is 20/10 s per channel — stay well under it.
            await asyncio.sleep(0.25)
            follow = await self.send(chat_id, chunk, metadata=metadata)
            if not follow.success:
                return SendResult(
                    success=False,
                    error=f"Fluxer: media delivered but caption continuation failed: {follow.error}",
                    retryable=follow.retryable,
                    error_kind=follow.error_kind,
                    message_id=message_id,
                    continuation_message_ids=tuple(message_ids),
                )
            if follow.message_id:
                message_ids.append(follow.message_id)
        return SendResult(
            success=True,
            message_id=message_id,
            continuation_message_ids=tuple(message_ids),
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively as an attachment."""
        return await self._send_media(
            chat_id,
            image_path,
            caption,
            metadata=metadata,
            reply_to=reply_to,
            kind="image",
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively: ``http(s)`` URL → download to a temp file first
        (Fluxer needs the bytes uploaded through the plan→PUT flow); a local path
        (or ``file://`` URL) uploads directly."""
        if self._rest is None:
            return SendResult(
                success=False,
                error="Fluxer: not connected",
                retryable=True,
                error_kind="transient",
            )
        source = unquote(str(image_url or ""))
        if source.startswith("file://"):
            source = unquote(source[7:])
        if source.startswith(("http://", "https://")):
            try:
                data = await download_attachment(source, max_bytes=MAX_ATTACHMENT_BYTES)
            except Exception as e:
                return SendResult(
                    success=False, error=f"Fluxer image download failed: {e}"
                )
            tmp_path: Optional[str] = None
            try:
                suffix = Path(urlsplit(source).path).suffix or ".jpg"
                with tempfile.NamedTemporaryFile(
                    prefix="fluxer_image_", suffix=suffix, delete=False
                ) as fh:
                    fh.write(data)
                    tmp_path = fh.name
                base_name = (
                    Path(unquote(urlsplit(source).path)).name or f"image{suffix}"
                )
                return await self._send_media(
                    chat_id,
                    tmp_path,
                    caption,
                    metadata=metadata,
                    reply_to=reply_to,
                    file_name=base_name,
                    kind="image",
                )
            finally:
                if tmp_path:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)
        return await self._send_media(
            chat_id, source, caption, metadata=metadata, reply_to=reply_to, kind="image"
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as an attachment, attempting the native VOICE_MESSAGE flag (8192).

        One attempt with the flag; if create-message 400s on it the flag is
        dropped and the message retried without it — live-verified requirements
        for a native voice note: no text content (``VOICE_MESSAGES_CANNOT_HAVE_CONTENT``)
        and ``waveform`` data on the attachment claim
        (``VOICE_MESSAGES_ATTACHMENT_WAVEFORM_REQUIRED``); the audio therefore
        still reaches the channel as a plain attachment.  Waveform synthesis is
        a voice-lane (W3/C4) follow-up.
        """
        return await self._send_media(
            chat_id,
            audio_path,
            caption,
            metadata=metadata,
            reply_to=reply_to,
            flags=VOICE_MESSAGE_FLAG,
            kind="audio",
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local video file natively as an attachment."""
        return await self._send_media(
            chat_id,
            video_path,
            caption,
            metadata=metadata,
            reply_to=reply_to,
            kind="video",
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an arbitrary file natively as an attachment (``file_name`` overrides
        the uploaded filename; the local path is never echoed to chat)."""
        return await self._send_media(
            chat_id,
            file_path,
            caption,
            metadata=metadata,
            reply_to=reply_to,
            file_name=file_name,
            kind="document",
        )

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Any],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> SendResult:
        """Send ``(url, alt)`` images bundled into messages of ≤10 attachments.

        URL entries are downloaded to temp files first (Fluxer renders only
        uploaded attachments).  Returns success when at least one image was
        delivered — the base contract (#106153: a media-only turn must not
        report FAILURE when images went out).
        """
        if not images:
            return SendResult(success=False, error="no images to send")
        rest = self._rest
        if rest is None:
            return SendResult(
                success=False,
                error="Fluxer: not connected",
                retryable=True,
                error_kind="transient",
            )
        entries: List[tuple] = []  # (path, is_temp, alt_text)
        for image_url, alt_text in images:
            source = unquote(str(image_url or ""))
            if source.startswith("file://"):
                source = unquote(source[7:])
            if source.startswith(("http://", "https://")):
                try:
                    data = await download_attachment(
                        source, max_bytes=MAX_ATTACHMENT_BYTES
                    )
                    suffix = Path(urlsplit(source).path).suffix or ".jpg"
                    with tempfile.NamedTemporaryFile(
                        prefix="fluxer_image_", suffix=suffix, delete=False
                    ) as fh:
                        fh.write(data)
                        entries.append((fh.name, True, alt_text or ""))
                except Exception as e:
                    logger.warning(
                        "Fluxer: skipping image %s: %s", source.split("?", 1)[0], e
                    )
            elif os.path.isfile(source):
                entries.append((source, False, alt_text or ""))
            else:
                logger.warning("Fluxer: skipping missing image %s", source)
        if not entries:
            return SendResult(success=False, error="all images failed to send")
        delivered = False
        try:
            batches = [
                entries[i : i + self.MAX_ATTACHMENTS_PER_MESSAGE]
                for i in range(0, len(entries), self.MAX_ATTACHMENTS_PER_MESSAGE)
            ]
            for batch_idx, batch in enumerate(batches):
                if human_delay > 0 and batch_idx > 0:
                    await asyncio.sleep(human_delay)
                claims: List[dict] = []
                for idx, (path, _is_temp, _alt) in enumerate(batch):
                    if idx:
                        # attachment-plan bucket: 10/10 s per user+channel.
                        await asyncio.sleep(0.15)
                    try:
                        claims.append(await rest.upload_attachment(chat_id, path))
                    except Exception as e:
                        logger.warning(
                            "Fluxer: image upload failed for %s: %s", path, e
                        )
                if not claims:
                    continue
                caption = next((alt for _p, _t, alt in batch if alt), None)
                try:
                    await rest.create_message(
                        chat_id, content=caption, attachments=claims
                    )
                    delivered = True
                except Exception as e:
                    logger.warning(
                        "Fluxer: multi-image message failed (%d attachment(s)): %s",
                        len(claims),
                        e,
                    )
        finally:
            for path, is_temp, _alt in entries:
                if is_temp:
                    with contextlib.suppress(OSError):
                        os.unlink(path)
        return SendResult(
            success=delivered, error=None if delivered else "all images failed to send"
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Typing indicator, self-throttled ≥8 s per chat (base keeps polling)."""
        rest = self._rest
        if rest is None:
            return
        chat_key = str(chat_id)
        now = time.monotonic()
        if now - self._last_typing.get(chat_key, 0.0) < TYPING_THROTTLE_SECONDS:
            return
        self._last_typing[chat_key] = now
        try:
            await rest.send_typing(chat_key)
        except Exception as e:
            logger.debug("Fluxer: typing indicator failed for %s: %s", chat_id, e)

    # ── voice channel integration (wave 3, spec §6-W3) ───────────────────
    # Method names/signatures mirror the discord adapter so the core probes in
    # gateway/slash_commands.py (`/voice join|leave|status`) and gateway/run_voice.py
    # find them via hasattr on ANY platform adapter.

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Play auto-TTS audio inside the bound voice channel, else send it as a file."""
        session = None
        if self._voice is not None and self._voice_cfg.enabled:
            session = self._voice.session_for_chat(chat_id)
        if session is not None:
            ok = await self._voice.speak_file(
                audio_path, session=session, tag="play_tts"
            )
            return SendResult(
                success=ok, error=None if ok else "Fluxer voice: playback failed"
            )
        return await self.send_voice(
            chat_id=chat_id, audio_path=audio_path, caption=caption, metadata=metadata
        )

    async def join_voice_channel(
        self,
        channel,
        *,
        text_channel_id: Optional[Any] = None,
        source: Optional[Any] = None,
    ) -> bool:
        """Join a voice channel (core ``/voice join`` path + programmatic joins).

        ``channel`` is a :class:`~.voice.controller.VoiceChannelRef` (or any object
        with ``id``/``guild_id``, e.g. the discord-shaped result of
        :meth:`get_user_voice_channel`).  ``source`` (a SessionSource/dict) binds
        ``session_binding: invoke`` to the invoking chat; without one the controller
        falls back to channel binding with a log line.
        """
        ctl = self._voice_controller()
        if ctl is None:
            return False
        guild_id = getattr(channel, "guild_id", None)
        if guild_id is None and getattr(channel, "guild", None) is not None:
            guild_id = getattr(channel.guild, "id", None)
        channel_id = getattr(channel, "id", None) or channel
        if not guild_id:
            logger.warning(
                "Fluxer voice: join_voice_channel without a guild id (channel=%r)",
                channel,
            )
            return False
        return await ctl.join(
            str(guild_id),
            str(channel_id),
            source=source or self._last_command_source or None,
            text_channel_id=text_channel_id,
        )

    async def leave_voice_channel(self, guild_id: Any) -> None:
        """Gateway op4 leave for the guild's active voice session (no-op when absent)."""
        ctl = self._voice
        if ctl is not None:
            await ctl.leave(str(guild_id), reason="core-leave")

    def is_in_voice_channel(self, guild_id: Any) -> bool:
        ctl = self._voice
        return bool(ctl is not None and ctl.is_in_channel(str(guild_id)))

    def get_voice_channel_info(self, guild_id: Any) -> Optional[Dict[str, Any]]:
        """``{channel_name, member_count, members:[{user_id,display_name,is_speaking}], …}``."""
        ctl = self._voice
        if ctl is None:
            return None
        return ctl.channel_info(str(guild_id))

    def get_voice_channel_context(self, guild_id: Any) -> str:
        """Human-readable voice context for prompt injection ('' when not in voice)."""
        info = self.get_voice_channel_info(guild_id)
        if not info:
            return ""
        parts = [
            f"[Voice channel: #{info['channel_name']} — {info['member_count']} participant(s)]"
        ]
        for member in info["members"]:
            status = " (speaking)" if member.get("is_speaking") else ""
            parts.append(f"  - {member['display_name']}{status}")
        return "\n".join(parts)

    async def get_user_voice_channel(
        self, guild_id: Any, user_id: Any
    ) -> Optional[VoiceChannelRef]:
        """Voice channel the user currently sits in (from routed VOICE_STATE_UPDATEs)."""
        ctl = self._voice_controller()
        if ctl is None:
            return None
        return await ctl.get_user_voice_channel(str(guild_id), str(user_id))

    # ── voice-mode context preamble (wave 5, voice UX) ────────────────────

    def _voice_input_prompt_for_chat(self, chat_id: Any) -> Optional[str]:
        """Voice-mode ephemeral preamble for turns in a voice-bound channel.

        Returns ``voice.input_prompt`` when the chat is bound to an active voice
        session (so replies will be spoken) and the prompt is configured; None
        otherwise (so no preamble is injected).  Used both by the cascade dispatch
        path (``cascade._dispatch`` → event.channel_prompt) and the
        ``_resolve_channel_prompt`` callback hook for the core voice-input path.
        """
        if not (self._voice is not None and self._voice_cfg.enabled):
            return None
        prompt = (self._voice_cfg.input_prompt or "").strip()
        if not prompt:
            return None
        try:
            session = self._voice.session_for_chat(str(chat_id))
        except Exception:
            session = None
        return prompt if session is not None else None

    def _resolve_channel_prompt(self, channel_id: Any) -> Optional[str]:
        """Called by the core's voice-input callback path (``run_voice.py``:268)
        to produce the per-turn ephemeral prompt for a voice-turn event.

        Delegates to ``_voice_input_prompt_for_chat`` — same seam, different entry.
        """
        return self._voice_input_prompt_for_chat(str(channel_id))

    # ── DM /voice commands (wave 5, voice UX) ─────────────────────────────

    async def _handle_dm_voice_command(
        self, *, content: str, chat_id: str, author_id: str, source: Any
    ) -> bool:
        """Handle ``/voice join|channel|leave`` from a DM where the core handler
        cannot (no guild context).  Resolves the user's current voice channel
        across all tracked guilds (``controller.find_user_voice_channel``) and
        joins/leaves the bound LiveKit room.

        Post a text reply into the DM with ``self._rest.create_message``.
        Returns True if handled (caller should return); returns False for bare
        ``/voice`` (toggle) or subcommands like ``on|off|tts|status`` that the
        core can service normally.
        """
        tokens = content.strip().split()
        if len(tokens) < 2:
            return False  # bare /voice → core toggle
        sub = tokens[1].rstrip("!?.").lower()
        if sub not in {"join", "channel", "leave"}:
            return False
        ctl = self._voice_controller()
        if ctl is None:
            try:
                await self._rest.create_message(
                    chat_id, content="Voice is disabled on this bot."
                )
            except Exception:
                pass
            return True
        if sub in {"join", "channel"}:
            ref = await ctl.find_user_voice_channel(author_id)
            if ref is None:
                reply = "You need to be in a voice channel first."
            else:
                ok = await self.join_voice_channel(
                    ref, source=source.to_dict() if source else None
                )
                if ok:
                    reply = (
                        f"Joined voice channel **{ref.name}**.\n"
                        "I'll speak my replies and listen to you. "
                        "Use `/voice leave` to disconnect."
                    )
                else:
                    reply = "Failed to join voice channel. Check bot permissions (Connect + Speak)."
        else:  # leave
            sessions = (
                ctl.sessions_snapshot() if hasattr(ctl, "sessions_snapshot") else []
            )
            if not sessions:
                reply = "Not in a voice channel."
            else:
                left: list[str] = []
                for sess in sessions:
                    gid = sess.get("guild_id")
                    if gid:
                        await self.leave_voice_channel(gid)
                        left.append(gid)
                reply = f"Left voice channel{' (guild ' + str(len(left)) + ')' if len(left) > 0 else ''}."
        try:
            await self._rest.create_message(chat_id, content=reply)
        except Exception as e:
            logger.warning("Fluxer: DM voice reply failed: %s", e)
        return True

    async def play_in_voice_channel(self, guild_id: Any, audio_path: str) -> bool:
        """Play a local audio file into the guild's voice channel (core voice replies)."""
        ctl = self._voice
        if ctl is None:
            return False
        session = ctl.session_for_guild(str(guild_id))
        if session is None:
            return False
        return await ctl.speak_file(
            audio_path, session=session, tag="play_in_voice_channel"
        )

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        rest = self._rest
        if rest is None:
            return SendResult(
                success=False,
                error="Fluxer: not connected",
                retryable=True,
                error_kind="transient",
            )
        try:
            response = await rest.edit_message(
                str(chat_id), str(message_id), content=content
            )
        except FluxerAPIError as e:
            return self._send_error(e, [])
        except Exception as e:
            return SendResult(
                success=False,
                error=f"Fluxer edit failed: {e}",
                retryable=True,
                error_kind=classify_send_error(e, str(e)),
            )
        return SendResult(
            success=True, message_id=str((response or {}).get("id") or message_id)
        )

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        rest = self._rest
        if rest is None:
            return False
        try:
            await rest.delete_message(str(chat_id), str(message_id))
            return True
        except Exception as e:
            logger.debug(
                "Fluxer: delete_message(%s, %s) failed: %s", chat_id, message_id, e
            )
            return False

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """REST lookup → ``{name, type: "dm"|"channel", chat_id}``."""
        name, chat_type = str(chat_id), "channel"
        if self._rest is not None:
            try:
                channel = await self._rest.get_channel(str(chat_id))
                if isinstance(channel, dict):
                    name = str(channel.get("name") or name)
                    chat_type = (
                        "dm" if channel.get("type") in _DM_CHANNEL_TYPES else "channel"
                    )
            except Exception as e:
                logger.debug("Fluxer: get_chat_info failed for %s: %s", chat_id, e)
        return {"name": name, "type": chat_type, "chat_id": str(chat_id)}


# ── registration helpers ─────────────────────────────────────────────────────


def check_requirements() -> bool:
    """PASSIVE probe: deps importable NOW + token present. Never installs anything."""
    try:
        import aiohttp  # noqa: F401
        import websockets  # noqa: F401
    except Exception:
        return False
    return _is_wellformed_token(_get_scoped_secret("FLUXER_BOT_TOKEN"))


def validate_config(config) -> bool:
    """Enough info to connect: a well-formed bot token in the environment."""
    return _is_wellformed_token(_get_scoped_secret("FLUXER_BOT_TOKEN"))


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env before adapter construction; ``None`` when the
    token is missing (caller then skips auto-enabling). ``home_channel`` becomes a HomeChannel."""
    token = _get_scoped_secret("FLUXER_BOT_TOKEN")
    if not token:
        return None
    seed: dict = {}
    if home := str(_get_scoped_secret("FLUXER_HOME_CHANNEL") or "").strip():
        seed["home_channel"] = {
            "chat_id": home,
            "name": str(_get_scoped_secret("FLUXER_HOME_CHANNEL_NAME") or home),
        }
    for env, key in (
        ("FLUXER_API_BASE", "api_base"),
        ("FLUXER_GATEWAY_URL", "gateway_url"),
    ):
        if value := str(_get_scoped_secret(env) or "").strip():
            seed[key] = value
    return seed


def interactive_setup() -> None:
    """``hermes gateway setup`` flow (lazy hermes_cli imports keep the plugin importable)."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Fluxer")
    existing = get_env_value("FLUXER_BOT_TOKEN")
    if existing:
        print_info("Fluxer: already configured (FLUXER_BOT_TOKEN set)")
        if not prompt_yes_no("Reconfigure Fluxer?", False):
            return
    print_info(
        "Connect Hermes to Fluxer (fluxer.app) as a bot.",
        "   Create a bot application, then paste its token (format: <id>.<secret>).",
    )
    token = prompt("Fluxer bot token", password=True, default="")
    if not (token or "").strip():
        print_warning("A bot token is required — skipping Fluxer setup")
        return
    save_env_value("FLUXER_BOT_TOKEN", token.strip())
    print()
    print_info("🔒 Access control: who may talk to the bot")
    if prompt_yes_no("Allow all Fluxer users to trigger the bot (dev only)?", False):
        save_env_value("FLUXER_ALLOW_ALL_USERS", "true")
        print_warning("⚠️  Open access — anyone who can see the bot can command it.")
    else:
        save_env_value("FLUXER_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed user IDs (comma-separated, empty denies everyone)", default=""
        )
        save_env_value("FLUXER_ALLOWED_USERS", (allowed or "").replace(" ", ""))
    print()
    if prompt_yes_no("Set a default home channel for cron / notifications?", False):
        if home := prompt(
            "Home channel ID", default=get_env_value("FLUXER_HOME_CHANNEL") or ""
        ):
            save_env_value("FLUXER_HOME_CHANNEL", home.strip())
        if name := prompt(
            "Home channel display name",
            default=get_env_value("FLUXER_HOME_CHANNEL_NAME") or "",
        ):
            save_env_value("FLUXER_HOME_CHANNEL_NAME", name.strip())
    print_success("Fluxer configuration saved")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


# ── standalone sender (cron without a live gateway) ──────────────────────────


def _sa_error(detail: str) -> Dict[str, Any]:
    return {"error": f"Fluxer standalone send: {detail}"}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """REST-only chunked send (no WS). ``media_files`` upload via ``FluxerREST.upload_attachment``
    when the client provides it; otherwise a clear error — never a fake success."""
    extra = getattr(pconfig, "extra", {}) or {}
    token = str(_get_scoped_secret("FLUXER_BOT_TOKEN") or "").strip()
    if not token:
        return _sa_error("FLUXER_BOT_TOKEN is not set")
    base_url = str(
        _get_scoped_secret("FLUXER_API_BASE")
        or extra.get("api_base")
        or DEFAULT_API_BASE
    )
    rest = FluxerREST(token, base_url=base_url)
    try:
        chunks = BasePlatformAdapter.truncate_message(message or "", MAX_MESSAGE_LENGTH)
        if not chunks or all(not chunk.strip() for chunk in chunks):
            return _sa_error("empty message")
        attachments = None
        if media_files:
            upload = getattr(rest, "upload_attachment", None)
            if not callable(upload):
                return _sa_error(
                    "media_files unsupported by this client build (no upload_attachment)"
                )
            attachments = [await upload(chat_id, path) for path in media_files]
        last_id: Optional[str] = None
        for i, chunk in enumerate(chunks):
            response = await rest.create_message(
                chat_id,
                content=chunk,
                attachments=(attachments if (i == 0 and attachments) else None),
            )
            last_id = str((response or {}).get("id") or "") or last_id
            if len(chunks) > 1:
                await asyncio.sleep(0.25)
        return {"success": True, "message_id": last_id}
    except FluxerAPIError as e:
        return _sa_error(str(e))
    except Exception as e:
        logger.debug("Fluxer standalone send raised", exc_info=True)
        return _sa_error(f"{type(e).__name__}: {e}")
    finally:
        with contextlib.suppress(Exception):
            await rest.close()


# ── plugin registration ──────────────────────────────────────────────────────


def register(ctx):
    """Plugin entry point: called by the Hermes plugin system (spec §2.4)."""
    ctx.register_platform(
        name="fluxer",
        label="Fluxer",
        adapter_factory=FluxerAdapter,
        check_fn=check_requirements,  # passive: deps importable + token present
        ensure_deps_fn=None,  # aiohttp + websockets ship with Hermes
        validate_config=validate_config,  # token present & well-formed (contains ".")
        is_connected=is_connected,
        required_env=["FLUXER_BOT_TOKEN"],
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="FLUXER_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="FLUXER_ALLOWED_USERS",
        allow_all_env="FLUXER_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="⚡",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Fluxer (fluxer.app), a Discord-style messaging "
            "platform. Standard markdown and fenced code blocks render; messages are "
            "capped at 4000 characters (long replies are split automatically). In "
            "guild channels you are addressed by @mention (or a leading '/' command); "
            "in free-response channels every message reaches you. Keep replies "
            "concise and conversational."
        ),
    )
