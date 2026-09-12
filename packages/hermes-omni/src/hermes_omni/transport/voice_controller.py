"""Voice session controller for hermes-omni transport.

Owns the join/leave lifecycle (gateway op 4 → ``VOICE_SERVER_UPDATE`` → LiveKit
``Room.connect``), session binding, speech publishing (piper → 48 kHz PCM →
``AudioSource``), bounded re-op4 rejoin after room/token death,
``token_refreshed`` adoption and optional auto-leave when the channel stays
empty.

**LiveKit is imported lazily inside functions** — on ``ImportError`` the
controller stays inert, logs once ("voice disabled: install livekit …") and the
plugin keeps working text-only.  Tokens are never logged; only ``len``.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import inspect
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple, Union

from . import audio as audio_lib
from .voice_cascade import VoiceCascade, speaker_from_identity
from .config import VoiceConfig

log = logging.getLogger("hermes_omni.transport.voice_controller")

#: Participant/track names used on the wire.
TRACK_NAME = "hermes-voice"
SILENCE_TAIL_FRAMES = max(1, audio_lib.SILENCE_TAIL_MS // audio_lib.FRAME_MS)
#: Rejoin backoff (seconds): ``base * attempt`` capped at ``max`` (tests patch these).
REJOIN_BASE_DELAY = 2.0
REJOIN_MAX_DELAY = 15.0

# Lazy-livekit sentinel.
_LIVEKIT_CACHE: Any = None
_LIVEKIT_WARNED = False


def try_livekit() -> Any:
    """Lazy import of ``livekit.rtc``; returns the module or None (logs once on failure)."""
    global _LIVEKIT_CACHE, _LIVEKIT_WARNED  # noqa: PLW0603
    if _LIVEKIT_CACHE is not None:
        return _LIVEKIT_CACHE
    try:
        from livekit import rtc  # noqa: PLC0415 — deliberately lazy
        _LIVEKIT_CACHE = rtc
        return rtc
    except ImportError:
        if not _LIVEKIT_WARNED:
            _LIVEKIT_WARNED = True
            log.warning("voice disabled: install livekit package (pip install livekit)")
        return None
    except Exception as exc:
        if not _LIVEKIT_WARNED:
            _LIVEKIT_WARNED = True
            log.warning("voice disabled: livekit import failed: %s", exc)
        return None


class SessionController(Protocol):
    """Abstract interface for the platform adapter driving a voice session.

    Minimum contract for a controller that ``VoiceController`` and
    ``VoiceCascade`` can drive.  Concrete adapters (Discord, Telegram, …)
    must provide these attributes/methods.  Optional attributes are checked
    via ``getattr`` and gracefully degrade.
    """

    # Required — the bot's own user/application id.
    _bot_id: str

    async def _chat_name(self, channel_id: str) -> str:
        """Resolve a channel id to a display name."""
        ...

    def build_source(
        self, *, chat_id: str, chat_name: str, chat_type: str,
        user_id: str, user_name: str, scope_id: str, guild_id: str,
    ) -> Any:
        """Build a source metadata object for a given chat/user context."""
        ...

    async def handle_message(self, event: Any) -> None:
        """Dispatch a synthetic message event through the agent pipeline."""
        ...

    # Optional — these are checked via getattr and degrade gracefully:
    # _ws          — gateway websocket (needs update_voice_state())
    # _rest        — REST client (needs create_message())
    # _is_user_authorized(user_id) → bool
    # _voice_input_callback(guild_id, user_id, transcript)
    # _voice_text_channels → dict
    # _voice_input_prompt_for_chat(chat_id) → str | None


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 16:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.95


@dataclass
class VoiceChannelRef:
    """Minimal channel handle returned to voice helpers."""

    id: str
    guild_id: str
    name: str = ""


class VoiceSession:
    """One bound voice session (guild + voice channel)."""

    def __init__(self, *, guild_id: str, channel_id: str, binding_chat_id: str,
                 binding_chat_type: str, binding_chat_name: str, scope_id: Optional[str],
                 source_dict: Optional[dict], config: VoiceConfig) -> None:
        self.guild_id = str(guild_id)
        self.channel_id = str(channel_id)
        self.binding_chat_id = str(binding_chat_id)
        self.binding_chat_type = binding_chat_type
        self.binding_chat_name = binding_chat_name
        self.scope_id = scope_id
        self.source_dict = source_dict or {}
        self.config = config
        # runtime
        self.rtc: Any = None
        self.room: Any = None
        self.connection_id: Optional[str] = None
        self.source: Any = None
        self.track: Any = None
        self.publication: Any = None
        self.publication_sid: Optional[str] = None
        self.cascade: Optional[VoiceCascade] = None
        self.state = "joining"          # joining | connected | degraded | closed
        self.leaving = False
        self.rejoin_attempts = 0
        self.rejoin_active = False
        self.empty_since: Optional[float] = None
        self.speak_lock = asyncio.Lock()
        self.recent_spoken: List[Tuple[float, str]] = []
        self.speaking_users: Dict[str, float] = {}
        self.created_mono = time.monotonic()
        self.connected_mono: Optional[float] = None
        self.stats: Dict[str, int] = {
            "connects": 0, "frames_published": 0, "speak_ok": 0, "speak_failures": 0,
            "speak_deduped": 0, "speak_dropped_busy": 0, "room_disconnects": 0,
        }
        #: Optional callable ``(text: str, speaker_id: str) → Awaitable[None]``
        #: for posting STT transcripts into the bound channel.
        self.send_transcript: Optional[Callable[..., Any]] = None
        self._watcher_task: Optional[asyncio.Task] = None

    @property
    def echo_chat_id(self) -> Optional[str]:
        """Where ``transcripts: channel`` posts (None = nowhere postable)."""
        if self.binding_chat_type == "voice":
            return None
        return self.binding_chat_id

    def build_source(self, controller: SessionController, speaker_id: str, speaker_name: str):
        """``SessionSource`` for a transcript turn, per the session binding mode."""
        return controller.build_source(
            chat_id=self.binding_chat_id,
            chat_name=self.binding_chat_name or self.binding_chat_id,
            chat_type=self.binding_chat_type,
            user_id=speaker_id,
            user_name=speaker_name or speaker_id,
            scope_id=self.scope_id or self.guild_id,
            guild_id=self.guild_id,
        )


class VoiceController:
    """Join/leave/speak/rejoin brain for one platform adapter instance.

    Accepts a :class:`SessionController`-compatible adapter *or* a protocol
    object (plain object with the right attributes), plus optional piper/stt
    overrides for testing.
    """

    def __init__(
        self,
        controller: SessionController,
        config: VoiceConfig,
        *,
        piper_runner: Optional[Callable[[str, str], Tuple[bool, str]]] = None,
        stt_callable: Optional[Callable[[str], Optional[str]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.controller = controller
        self.config = config
        self._piper_override = piper_runner
        self._stt_override = stt_callable
        self._clock = clock
        self._sessions: Dict[str, VoiceSession] = {}          # channel_id → session
        self._by_chat: Dict[str, VoiceSession] = {}           # binding chat_id → session
        self._vsu_waiters: Dict[Tuple[str, str], asyncio.Future] = {}
        self._last_vsu: Dict[Tuple[str, str], dict] = {}
        self._voice_states: Dict[str, Dict[str, str]] = {}    # guild → user → channel
        self._member_names: Dict[str, str] = {}
        self._tasks: Set[asyncio.Task] = set()
        self._closing = False
        self.stats: Dict[str, int] = {
            "joins": 0, "join_failures": 0, "leaves": 0, "op4_joins": 0,
            "room_disconnects": 0, "rejoins_ok": 0, "rejoin_failures": 0,
            "token_refreshed": 0, "speak_ok": 0, "speak_failures": 0,
            "speak_deduped": 0, "speak_dropped_busy": 0,
        }

    # ── livekit gating ───────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def rtc_module(self):
        """The ``livekit.rtc`` module or None (logs once when missing)."""
        return try_livekit()

    # ── gateway event routing ────────────────────────────────────────────

    def on_gateway_event(self, event_type: str, data: Any) -> None:
        """Route voice-relevant gateway events (called from the adapter)."""
        data = data if isinstance(data, dict) else {}
        if event_type == "VOICE_SERVER_UPDATE":
            self._on_voice_server_update(data)
        elif event_type == "VOICE_STATE_UPDATE":
            self._on_voice_state_update(data)
        elif event_type == "GUILD_CREATE":
            self.seed_from_guild(data)

    def _on_voice_server_update(self, data: dict) -> None:
        guild = str(data.get("guild_id") or "")
        channel = str(data.get("channel_id") or "")
        key = (guild, channel)
        self._last_vsu[key] = data
        log.info(
            "hermes-omni: VOICE_SERVER_UPDATE guild=%s channel=%s connection=%s "
            "endpoint_host=%s token_len=%s",
            guild, channel, data.get("connection_id"),
            str(data.get("endpoint") or "").split("/")[-1],
            len(str(data.get("token") or "")),
        )
        waiter = self._vsu_waiters.get(key)
        if waiter is not None and not waiter.done():
            waiter.set_result(data)

    def _on_voice_state_update(self, data: dict) -> None:
        guild = str(data.get("guild_id") or "")
        user = str(data.get("user_id") or "")
        channel = data.get("channel_id")
        if guild and user:
            if channel:
                self._voice_states.setdefault(guild, {})[user] = str(channel)
            else:
                self._voice_states.get(guild, {}).pop(user, None)
        bot_id = str(getattr(self.controller, "_bot_id", "") or "")
        if user and bot_id and user == bot_id and not channel:
            session = self.session_for_guild(guild)
            if session is not None and not session.leaving and not self._closing \
                    and not session.rejoin_active:
                log.warning(
                    "hermes-omni: gateway reports the bot left %s/%s — scheduling rejoin",
                    guild, session.channel_id,
                )
                self._schedule_rejoin(session, "voice_state_left")

    def seed_from_guild(self, data: dict) -> None:
        guild = str(data.get("id") or "")
        if not guild:
            return
        for vs in data.get("voice_states") or []:
            if not isinstance(vs, dict):
                continue
            user = str(vs.get("user_id") or "")
            channel = vs.get("channel_id")
            if user and channel:
                self._voice_states.setdefault(guild, {})[user] = str(channel)
        for member in data.get("members") or []:
            if not isinstance(member, dict):
                continue
            user = member.get("user") or {}
            uid = str(user.get("id") or "")
            if uid:
                self._member_names[uid] = str(
                    user.get("display_name") or user.get("username") or uid)

    def seed_from_ready(self, payload: Any) -> None:
        """Seed voice states/member names from the gateway READY payload."""
        if not isinstance(payload, dict):
            return
        for guild in payload.get("guilds") or []:
            if isinstance(guild, dict):
                self.seed_from_guild(guild)

    def voice_states_snapshot(self) -> Dict[str, Dict[str, str]]:
        return {guild: dict(states) for guild, states in self._voice_states.items()}

    # ── session lookup ───────────────────────────────────────────────────

    def session_for_chat(self, chat_id: Any) -> Optional[VoiceSession]:
        session = self._by_chat.get(str(chat_id))
        if session is not None and not session.leaving:
            return session
        return None

    def session_for_channel(self, channel_id: Any) -> Optional[VoiceSession]:
        session = self._sessions.get(str(channel_id))
        if session is not None and not session.leaving:
            return session
        return None

    def session_for_guild(self, guild_id: Any) -> Optional[VoiceSession]:
        for session in self._sessions.values():
            if session.guild_id == str(guild_id) and not session.leaving:
                return session
        return None

    def is_in_channel(self, guild_id: Any) -> bool:
        session = self.session_for_guild(guild_id)
        return bool(session is not None and session.state == "connected")

    def sessions_snapshot(self) -> List[dict]:
        return [{
            "guild_id": s.guild_id, "channel_id": s.channel_id,
            "binding_chat_id": s.binding_chat_id, "binding_chat_type": s.binding_chat_type,
            "state": s.state, "connection_id": s.connection_id,
            "rejoin_attempts": s.rejoin_attempts, "publication_sid": s.publication_sid,
            **s.stats,
        } for s in self._sessions.values()]

    # ── join / leave ─────────────────────────────────────────────────────

    async def start_auto_channels(self) -> List[bool]:
        """Join every configured ``auto_channels`` entry (bounded, sequential)."""
        results: List[bool] = []
        for guild_id, channel_id in self.config.auto_channels:
            if not self.config.auto_join_allowed(channel_id):
                log.warning("hermes-omni: auto_channels entry %s/%s is not in voice.channels — skipped",
                            guild_id, channel_id)
                results.append(False)
                continue
            ok = await self.join(guild_id, channel_id, source=None)
            log.info("hermes-omni: auto-join %s/%s → %s", guild_id, channel_id, "ok" if ok else "failed")
            results.append(ok)
        return results

    async def join(self, guild_id: Any, channel_id: Any, *, source: Any = None,
                   text_channel_id: Any = None) -> bool:
        """Join (or move to) a voice channel; returns True once the room is connected."""
        if not self.enabled:
            log.info("hermes-omni: join refused (voice.enabled=false)")
            return False
        rtc = self.rtc_module()
        if rtc is None:
            return False
        guild_id, channel_id = str(guild_id), str(channel_id)
        if self.config.channels and channel_id not in self.config.channels:
            log.warning("hermes-omni: joining %s is not allowed by voice.channels (%s)",
                        channel_id, ",".join(sorted(self.config.channels)))
            return False
        existing = self.session_for_channel(channel_id)
        if existing is not None and existing.state == "connected":
            log.info("hermes-omni: already in %s", channel_id)
            return True

        # one session per guild: moving = teardown old (op4 to the new channel replaces it)
        previous = self.session_for_guild(guild_id)
        if previous is not None:
            log.info("hermes-omni: moving from %s to %s", previous.channel_id, channel_id)
            await self._teardown_session(previous, reason="move", send_op4=False)

        session = self._make_session(guild_id, channel_id, source=source,
                                     text_channel_id=text_channel_id)
        session.rtc = rtc
        self._register_session(session)
        try:
            vsu = await self._op4_join_and_wait(session)
            await self._connect_room(session, vsu)
        except Exception as e:
            self.stats["join_failures"] += 1
            log.error("hermes-omni: join %s/%s failed: %s", guild_id, channel_id, e)
            await self._teardown_session(session, reason="join-failed")
            return False
        self.stats["joins"] += 1
        self._start_watchers(session)
        log.info(
            "hermes-omni: joined guild=%s channel=%s binding=%s:%s connection=%s",
            guild_id, channel_id, session.binding_chat_type, session.binding_chat_id,
            session.connection_id,
        )
        return True

    def _make_session(self, guild_id: str, channel_id: str, *, source: Any,
                      text_channel_id: Any) -> VoiceSession:
        """Resolve ``session_binding`` → binding chat identity (+ documented fallbacks)."""
        mode = self.config.session_binding
        source_dict: Optional[dict] = None
        fallback_reason: Optional[str] = None
        if mode == "invoke":
            if source is not None:
                source_dict = source.to_dict() if hasattr(source, "to_dict") else dict(source)
            elif text_channel_id:
                # The invoke context is the channel the command arrived from.
                source_dict = {"chat_id": str(text_channel_id), "chat_type": "channel"}
                fallback_reason = "no source object; used text_channel_id"
            else:
                fallback_reason = "no invoke source available"
        if mode in ("ephemeral", "invoke") and mode == "invoke" and source_dict is None:
            fallback_reason = fallback_reason or "invoke source missing"
            log.warning("hermes-omni: session_binding=invoke but no source was supplied — "
                        "falling back to channel binding (%s)", fallback_reason)
            mode = "channel"

        if mode == "ephemeral" or mode == "invoke":
            if mode == "ephemeral":
                chat_id = f"voice:{channel_id}:connecting"
                chat_type = "voice"
                chat_name = f"voice-{channel_id}"
            else:
                chat_id = str(source_dict.get("chat_id") or channel_id)
                chat_type = str(source_dict.get("chat_type") or "channel")
                chat_name = str(source_dict.get("chat_name") or chat_id)
        else:  # channel (default)
            chat_id = channel_id
            chat_type = "channel"
            chat_name = ""
        scope_id: Optional[str] = None
        if source_dict:
            scope_id = str(source_dict.get("scope_id") or "").strip() or None
        return VoiceSession(
            guild_id=guild_id, channel_id=channel_id, binding_chat_id=chat_id,
            binding_chat_type=chat_type, binding_chat_name=chat_name,
            scope_id=scope_id, source_dict=source_dict, config=self.config,
        )

    def _register_session(self, session: VoiceSession) -> None:
        self._sessions[session.channel_id] = session
        self._by_chat[session.binding_chat_id] = session

    async def leave(self, guild_id: Any, *, reason: str = "requested") -> bool:
        session = self.session_for_guild(guild_id)
        if session is None:
            return False
        await self._teardown_session(session, reason=reason)
        return True

    async def leave_chat(self, chat_id: Any, *, reason: str = "requested") -> bool:
        session = self.session_for_chat(chat_id)
        if session is None:
            return False
        await self._teardown_session(session, reason=reason)
        return True

    async def shutdown(self) -> None:
        """Leave every session (called from adapter teardown, before the WS stops)."""
        self._closing = True
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._teardown_session(session, reason="shutdown"), 12)
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def _teardown_session(self, session: VoiceSession, *, reason: str,
                                send_op4: bool = True) -> None:
        if session.state == "closed":
            return
        session.leaving = True
        session.state = "closed"
        if session.cascade is not None:
            with contextlib.suppress(Exception):
                await session.cascade.stop()
        if session._watcher_task is not None:
            session._watcher_task.cancel()
            session._watcher_task = None
        if send_op4:
            ws = getattr(self.controller, "_ws", None)
            if ws is not None:
                try:
                    await ws.update_voice_state(session.guild_id, None)
                    self.stats["op4_leaves"] = self.stats.get("op4_leaves", 0) + 1
                except Exception as e:
                    log.warning("hermes-omni: op4 leave failed: %s", e)
        if session.room is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.room.disconnect(), 8)
        if self._sessions.get(session.channel_id) is session:
            self._sessions.pop(session.channel_id, None)
        if self._by_chat.get(session.binding_chat_id) is session:
            self._by_chat.pop(session.binding_chat_id, None)
        self.stats["leaves"] += 1
        log.info("hermes-omni: left %s/%s (%s)", session.guild_id, session.channel_id, reason)

    # ── op4 / room connect ───────────────────────────────────────────────

    async def _op4_join_and_wait(self, session: VoiceSession) -> dict:
        ws = getattr(self.controller, "_ws", None)
        if ws is None:
            raise RuntimeError("gateway client not connected")
        key = (session.guild_id, session.channel_id)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._vsu_waiters[key] = waiter
        try:
            await ws.update_voice_state(session.guild_id, session.channel_id,
                                        self_mute=False, self_deaf=False)
            self.stats["op4_joins"] += 1
            vsu = await asyncio.wait_for(waiter, self.config.join_timeout_s)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"no VOICE_SERVER_UPDATE for {session.channel_id} within "
                f"{self.config.join_timeout_s:g}s") from None
        finally:
            self._vsu_waiters.pop(key, None)
        if not isinstance(vsu, dict) or not vsu.get("token") or not vsu.get("endpoint"):
            raise RuntimeError("VOICE_SERVER_UPDATE missing token/endpoint")
        return vsu

    async def _connect_room(self, session: VoiceSession, vsu: dict) -> None:
        rtc = session.rtc or self.rtc_module()
        if rtc is None:
            raise RuntimeError("livekit unavailable")
        endpoint = str(vsu.get("endpoint") or "")
        url = endpoint if endpoint.startswith(("ws://", "wss://")) else f"wss://{endpoint}"
        token = str(vsu.get("token") or "")
        room = rtc.Room()
        session.room = room
        self._wire_room(session, room)
        try:
            await asyncio.wait_for(room.connect(url, token), self.config.join_timeout_s + 10.0)
        finally:
            vsu.pop("token", None)  # never retain the grant after connect
        session.connection_id = str(vsu.get("connection_id") or "") or session.connection_id
        if session.binding_chat_type == "voice":
            new_binding = f"voice:{session.channel_id}:{session.connection_id or 'na'}"
            if new_binding != session.binding_chat_id:
                if self._by_chat.get(session.binding_chat_id) is session:
                    self._by_chat.pop(session.binding_chat_id, None)
                session.binding_chat_id = new_binding
                self._by_chat[new_binding] = session
        try:
            sid = room.sid
            if inspect.isawaitable(sid):
                sid = await sid
            sid = str(sid)
        except Exception:
            sid = "unavailable"
        session.stats["connects"] += 1
        session.state = "connected"
        session.connected_mono = time.monotonic()
        if session.cascade is None:
            session.cascade = VoiceCascade(session, self.controller, self.config,
                                           stt=self._stt_override)
        session.cascade.start()
        log.info(
            "hermes-omni: room connected name=%s sid=%s state=%s local=%s remotes=%d",
            getattr(room, "name", ""), sid, _enum_str(rtc, "ConnectionState", getattr(room, "connection_state", None)),
            getattr(getattr(room, "local_participant", None), "identity", "?"),
            len(getattr(room, "remote_participants", {}) or {}),
        )

    def _wire_room(self, session: VoiceSession, room: Any) -> None:
        def _safe(fn):
            def handler(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    log.exception("hermes-omni: room handler failed")
            return handler

        try:
            room.on("track_subscribed", _safe(
                lambda track, publication, participant: session.cascade.on_track_subscribed(
                    track, publication, participant) if session.cascade else None))
            room.on("disconnected", _safe(lambda reason: self._on_room_disconnected(session, reason)))
            room.on("token_refreshed", _safe(lambda *a: self._on_token_refreshed(session)))
            room.on("participant_connected", _safe(
                lambda p: self._on_participant_event(session, p, joined=True)))
            room.on("participant_disconnected", _safe(
                lambda p: self._on_participant_event(session, p, joined=False)))
            room.on("active_speakers_changed", _safe(
                lambda speakers: self._on_active_speakers(session, speakers)))
            room.on("reconnecting", _safe(lambda *a: log.info(
                "hermes-omni: room reconnecting channel=%s", session.channel_id)))
            room.on("reconnected", _safe(lambda *a: log.info(
                "hermes-omni: room reconnected channel=%s", session.channel_id)))
            room.on("connection_state_changed", _safe(
                lambda state: log.debug("hermes-omni: connection_state=%s channel=%s",
                                        state, session.channel_id)))
        except Exception as e:  # livekit version without one of the events: keep going
            log.warning("hermes-omni: room handler registration issue: %s", e)

    # ── room events ──────────────────────────────────────────────────────

    def _on_room_disconnected(self, session: VoiceSession, reason: Any) -> None:
        session.stats["room_disconnects"] += 1
        self.stats["room_disconnects"] += 1
        if session.leaving or self._closing:
            log.info("hermes-omni: room disconnected (intentional) channel=%s", session.channel_id)
            return
        log.warning("hermes-omni: room disconnected unexpectedly channel=%s reason=%s",
                    session.channel_id, reason)
        self._schedule_rejoin(session, f"room_disconnected:{reason}")

    def _on_token_refreshed(self, session: VoiceSession) -> None:
        session.stats["token_refreshed"] = session.stats.get("token_refreshed", 0) + 1
        self.stats["token_refreshed"] += 1
        log.info("hermes-omni: token_refreshed (adopted silently by the SDK) channel=%s",
                 session.channel_id)

    def _on_participant_event(self, session: VoiceSession, participant: Any, *, joined: bool) -> None:
        identity = getattr(participant, "identity", "?")
        if joined:
            session.empty_since = None
        log.info("hermes-omni: participant %s %s (channel=%s)",
                 "joined" if joined else "left", identity, session.channel_id)

    def _on_active_speakers(self, session: VoiceSession, speakers: Any) -> None:
        now = self._clock()
        for participant in speakers or []:
            user_id, _name = speaker_from_identity(getattr(participant, "identity", ""))
            if user_id:
                session.speaking_users[user_id] = now

    # ── rejoin (re-op4 strategy: same channel reconnect) ─────────────────

    def _schedule_rejoin(self, session: VoiceSession, reason: str) -> None:
        if session.rejoin_active or session.leaving or self._closing:
            return
        session.rejoin_active = True
        session.state = "degraded"
        self._spawn(self._rejoin(session, reason))

    async def _rejoin(self, session: VoiceSession, reason: str) -> None:
        try:
            if self.config.rejoin_max_attempts <= 0:
                log.error("hermes-omni: rejoin disabled (rejoin_max_attempts=0) — leaving %s",
                          session.channel_id)
                await self._teardown_session(session, reason=f"no-rejoin:{reason}")
                return
            while not session.leaving and not self._closing:
                if session.rejoin_attempts >= self.config.rejoin_max_attempts:
                    self.stats["rejoin_failures"] += 1
                    log.error("hermes-omni: rejoin attempts exhausted for %s/%s — leaving",
                              session.guild_id, session.channel_id)
                    await self._teardown_session(session, reason="rejoin-exhausted")
                    return
                session.rejoin_attempts += 1
                delay = min(REJOIN_BASE_DELAY * session.rejoin_attempts, REJOIN_MAX_DELAY)
                log.info("hermes-omni: rejoin %s attempt %d/%d in %.1fs (%s)",
                         session.channel_id, session.rejoin_attempts,
                         self.config.rejoin_max_attempts, delay, reason)
                await asyncio.sleep(delay)
                if session.leaving or self._closing:
                    return
                with contextlib.suppress(Exception):
                    await session.room.disconnect()
                try:
                    vsu = await self._op4_join_and_wait(session)
                    await self._connect_room(session, vsu)
                except Exception as e:
                    log.warning("hermes-omni: rejoin attempt failed: %s", e)
                    continue
                session.rejoin_attempts = 0
                session.state = "connected"
                self.stats["rejoins_ok"] += 1
                log.info("hermes-omni: rejoined %s/%s ok", session.guild_id, session.channel_id)
                return
        finally:
            session.rejoin_active = False

    # ── speech publishing ────────────────────────────────────────────────

    async def speak(self, text: str, *, chat_id: Any = None, session: Optional[VoiceSession] = None,
                    tag: str = "send-hook") -> bool:
        """Synthesize ``text`` with piper and publish it into the bound voice channel."""
        session = session or (self.session_for_chat(chat_id) if chat_id is not None else None)
        if session is None or session.leaving:
            return False
        text = (text or "").strip()
        if not text:
            return False
        if self._is_duplicate_speech(session, text):
            session.stats["speak_deduped"] += 1
            self.stats["speak_deduped"] += 1
            log.info("hermes-omni: skipping duplicate speech [%s] channel=%s",
                     tag, session.channel_id)
            return True
        if session.speak_lock.locked():
            if not self.config.queue_utterance:
                session.stats["speak_dropped_busy"] += 1
                self.stats["speak_dropped_busy"] += 1
                log.info("hermes-omni: dropping speech [%s] — previous utterance still playing", tag)
                return False
            session.stats["speak_queued"] = session.stats.get("speak_queued", 0) + 1
            self.stats["speak_queued"] = self.stats.get("speak_queued", 0) + 1
            log.info("hermes-omni: queueing speech [%s] — waiting for previous utterance", tag)
        try:
            return await asyncio.wait_for(
                self._speak_locked(session, text, tag), self.config.speak_timeout_s)
        except asyncio.TimeoutError:
            session.stats["speak_failures"] += 1
            self.stats["speak_failures"] += 1
            log.warning("hermes-omni: speech timed out after %.0fs [%s]",
                        self.config.speak_timeout_s, tag)
            return False
        except Exception as e:
            session.stats["speak_failures"] += 1
            self.stats["speak_failures"] += 1
            log.warning("hermes-omni: speech failed [%s]: %s", tag, e)
            return False

    async def speak_file(self, audio_path: Any, *, chat_id: Any = None,
                         session: Optional[VoiceSession] = None, tag: str = "play_tts") -> bool:
        """Publish an existing audio file (auto-TTS / ``play_in_voice_channel`` path)."""
        session = session or (self.session_for_chat(chat_id) if chat_id is not None else None)
        if session is None or session.leaving:
            return False
        path = Path(str(audio_path))
        if not path.is_file():
            log.warning("hermes-omni: speak_file missing file %s", path)
            return False
        if session.speak_lock.locked():
            if not self.config.queue_utterance:
                session.stats["speak_dropped_busy"] += 1
                log.info("hermes-omni: dropping playback [%s] — previous still playing", tag)
                return False
            session.stats["speak_queued"] = session.stats.get("speak_queued", 0) + 1
            log.info("hermes-omni: queueing playback [%s] — waiting for previous utterance", tag)
        try:
            async with session.speak_lock:
                samples, info = await asyncio.to_thread(audio_lib.load_wav_48k_mono, path)
                if not samples:
                    return False
                return await self._publish_samples(session, samples, tag)
        except Exception as e:
            session.stats["speak_failures"] += 1
            log.warning("hermes-omni: speak_file failed [%s]: %s", tag, e)
            return False

    async def _speak_locked(self, session: VoiceSession, text: str, tag: str) -> bool:
        async with session.speak_lock:
            tmp_dir = Path(tempfile.mkdtemp(prefix="hermes-omni-tts-"))
            wav_path = tmp_dir / "speech.wav"
            try:
                runner = self._piper_override or self._run_piper
                ok, detail = await asyncio.to_thread(runner, text, str(wav_path))
                if not ok:
                    session.stats["speak_failures"] += 1
                    self.stats["speak_failures"] += 1
                    log.warning("hermes-omni: piper synthesis failed [%s]: %s", tag, detail)
                    return False
                samples, info = await asyncio.to_thread(audio_lib.load_wav_48k_mono, wav_path)
                if not samples:
                    session.stats["speak_failures"] += 1
                    return False
                log.info("hermes-omni: piper WAV %.2fs (%sHz→48k) [%s]",
                         info.out_seconds, info.src_rate, tag)
                ok = await self._publish_samples(session, samples, tag)
                if ok:
                    session.recent_spoken.append((self._clock(), _normalize_text(text)))
                    self.stats["speak_ok"] += 1
                return ok
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _run_piper(self, text: str, out_path: str) -> Tuple[bool, str]:
        binary = Path(self.config.piper_binary)
        model = Path(self.config.piper_model)
        if not binary.is_file():
            return False, f"piper binary not found at {binary} (set voice.tts.binary)"
        if not model.is_file():
            return False, f"piper model not found at {model} (set voice.tts.model)"
        try:
            proc = subprocess.run(
                [str(binary), "--model", str(model), "--output_file", out_path],
                input=text.encode("utf-8"), capture_output=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            return False, "piper timed out"
        if proc.returncode != 0 or not os.path.isfile(out_path):
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-2:]
            return False, f"piper exit {proc.returncode}: {' | '.join(tail)}"
        return True, out_path

    async def _ensure_publisher(self, session: VoiceSession) -> bool:
        if session.source is not None and session.publication is not None:
            return True
        rtc = session.rtc
        room = session.room
        if rtc is None or room is None:
            return False
        try:
            source = rtc.AudioSource(audio_lib.SAMPLE_RATE, 1)
            track = rtc.LocalAudioTrack.create_audio_track(TRACK_NAME, source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            publication = await asyncio.wait_for(
                room.local_participant.publish_track(track, options), 15.0)
        except Exception as e:
            session.stats["speak_failures"] += 1
            log.warning("hermes-omni: publish_track failed: %s", e)
            return False
        session.source, session.track, session.publication = source, track, publication
        session.publication_sid = str(getattr(publication, "sid", "") or "") or None
        log.info("hermes-omni: published track name=%s sid=%s source=mic",
                 getattr(publication, "name", TRACK_NAME), session.publication_sid)
        return True

    async def _publish_samples(self, session: VoiceSession, samples: array.array,
                               tag: str) -> bool:
        rtc = session.rtc
        if rtc is None or session.room is None or session.leaving:
            return False
        if not await self._ensure_publisher(session):
            return False
        started = self._clock()
        frames = audio_lib.pad_frames(samples)
        silence = array.array("h", [0] * audio_lib.FRAME_SAMPLES)
        try:
            for chunk in frames:
                frame = rtc.AudioFrame.create(audio_lib.SAMPLE_RATE, 1, audio_lib.FRAME_SAMPLES)
                audio_lib.put_samples(frame, chunk)
                await session.source.capture_frame(frame)
            for _ in range(SILENCE_TAIL_FRAMES):
                frame = rtc.AudioFrame.create(audio_lib.SAMPLE_RATE, 1, audio_lib.FRAME_SAMPLES)
                audio_lib.put_samples(frame, silence)
                await session.source.capture_frame(frame)
        except Exception as e:
            session.stats["speak_failures"] += 1
            log.warning("hermes-omni: frame publish failed [%s]: %s", tag, e)
            return False
        total = len(frames) + SILENCE_TAIL_FRAMES
        session.stats["frames_published"] += total
        session.stats["speak_ok"] += 1
        log.info("hermes-omni: published %d frames (%d speech + %d silence tail) in %.2fs [%s]",
                 total, len(frames), SILENCE_TAIL_FRAMES, self._clock() - started, tag)
        wait_for_playout = getattr(session.source, "wait_for_playout", None)
        if callable(wait_for_playout):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(wait_for_playout(), 5.0)
        return True

    def _is_duplicate_speech(self, session: VoiceSession, text: str) -> bool:
        window = self.config.speak_dedupe_s
        if window <= 0:
            return False
        now = self._clock()
        norm = _normalize_text(text)
        session.recent_spoken = [(ts, old) for ts, old in session.recent_spoken
                                 if now - ts <= window]
        return any(_similar(old, norm) for _ts, old in session.recent_spoken)

    # ── cancel (interrupt current TTS, reset VAD state) ──────────────────

    def cancel(self, guild_id: Any) -> None:
        """Cancel the current TTS playback and reset VAD state for a guild.

        Stops the current synthesis/publishing pipeline by releasing the speak
        lock (so the next utterance can proceed) and resetting the VAD segmenter
        in the cascade (so any half-heard speech is discarded).
        """
        session = self.session_for_guild(guild_id)
        if session is None:
            return
        # Cancel any in-flight cascade task (VAD state reset).
        if session.cascade is not None:
            session.cascade.segmenter.flush()
            session.cascade.segmenter._reset()
        # Queue a cancellation onto the speak lock so the current TTS yields.
        # The lock itself is async — we schedule this as a fire-and-forget task.
        async def _release():
            session.speak_lock = asyncio.Lock()
        self._spawn(_release())
        log.info("hermes-omni: cancelled voice session guild=%s channel=%s",
                 session.guild_id, session.channel_id)

    # ── info helpers (voice status) ──────────────────────────────────────

    def channel_info(self, guild_id: Any) -> Optional[Dict[str, Any]]:
        session = self.session_for_guild(guild_id)
        if session is None or session.state != "connected":
            return None
        now = self._clock()
        members: List[dict] = []
        seen: Set[str] = set()
        states = self._voice_states.get(session.guild_id, {})
        bot_id = str(getattr(self.controller, "_bot_id", "") or "")
        for user_id, channel_id in states.items():
            if channel_id != session.channel_id or user_id == bot_id:
                continue
            seen.add(user_id)
            members.append({
                "user_id": user_id,
                "display_name": self._member_names.get(user_id, user_id),
                "is_speaking": (now - session.speaking_users.get(user_id, 0.0)) <= 2.0,
            })
        try:
            for participant in (session.room.remote_participants or {}).values():
                user_id, name = speaker_from_identity(getattr(participant, "identity", ""))
                if not user_id or user_id in seen or user_id == bot_id:
                    continue
                seen.add(user_id)
                members.append({
                    "user_id": user_id,
                    "display_name": self._member_names.get(user_id, name),
                    "is_speaking": (now - session.speaking_users.get(user_id, 0.0)) <= 2.0,
                })
        except Exception:
            pass
        return {
            "channel_name": session.binding_chat_name or session.channel_id,
            "member_count": len(members),
            "members": members,
            "speaking_count": sum(1 for m in members if m["is_speaking"]),
        }

    async def get_user_voice_channel(self, guild_id: Any, user_id: Any) -> Optional[VoiceChannelRef]:
        guild, user = str(guild_id), str(user_id)
        channel_id = self._voice_states.get(guild, {}).get(user)
        if not channel_id:
            return None
        name = channel_id
        name_fn = getattr(self.controller, "_chat_name", None)
        if callable(name_fn):
            with contextlib.suppress(Exception):
                name = str(await name_fn(channel_id) or channel_id)
        return VoiceChannelRef(id=str(channel_id), guild_id=guild, name=name)

    async def find_user_voice_channel(self, user_id: Any) -> Optional[VoiceChannelRef]:
        """Voice channel the user sits in, scanned across ALL tracked guilds.

        Useful for ``/voice join`` from a DM context where the invoker's guild
        is not known — the controller tracks every guild's VOICE_STATE_UPDATEs
        (see ``voice_states_snapshot()``).
        """
        user = str(user_id)
        for guild, states in self._voice_states.items():
            channel_id = states.get(user)
            if channel_id:
                name = channel_id
                name_fn = getattr(self.controller, "_chat_name", None)
                if callable(name_fn):
                    with contextlib.suppress(Exception):
                        name = str(await name_fn(channel_id) or channel_id)
                return VoiceChannelRef(id=str(channel_id), guild_id=guild, name=name)
        return None

    # ── watchers / tasks ─────────────────────────────────────────────────

    def _start_watchers(self, session: VoiceSession) -> None:
        if self.config.auto_leave_after_s > 0 and session._watcher_task is None:
            session._watcher_task = self._spawn(self._watch_empty(session))

    async def _watch_empty(self, session: VoiceSession) -> None:
        try:
            while not session.leaving and not self._closing:
                await asyncio.sleep(5.0)
                if self.config.auto_leave_after_s <= 0 or session.room is None:
                    continue
                try:
                    remote = len(getattr(session.room, "remote_participants", {}) or {})
                except Exception:
                    continue
                if remote == 0:
                    if session.empty_since is None:
                        session.empty_since = self._clock()
                    elif self._clock() - session.empty_since >= self.config.auto_leave_after_s:
                        log.info("hermes-omni: channel %s empty for %ds — leaving",
                                 session.channel_id, self.config.auto_leave_after_s)
                        await self._teardown_session(session, reason="empty")
                        return
                else:
                    session.empty_since = None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hermes-omni: empty-channel watcher failed")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def _enum_str(rtc, enum_name: str, value: Any) -> str:
    """Readable enum name across proto/python enum shapes."""
    if value is None:
        return "?"
    enum_cls = getattr(rtc, enum_name, None)
    if enum_cls is not None:
        with contextlib.suppress(Exception):
            return enum_cls.Name(value)  # protobuf EnumTypeWrapper
        with contextlib.suppress(Exception):
            return enum_cls(value).name  # python enum
    return str(value)