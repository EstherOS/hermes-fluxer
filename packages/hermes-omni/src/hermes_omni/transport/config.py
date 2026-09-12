"""Voice-lane configuration for hermes-omni (transport layer).

All knobs are optional; parsing is total (never raises) so a typo in config
cannot break load.  Bad values fall back to defaults with a warning naming
the key and the rejected value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

log = logging.getLogger("hermes_omni.transport.config")

BINDING_MODES = ("ephemeral", "channel", "invoke")
TRANSCRIPT_MODES = ("off", "channel")
STT_ENGINES = ("hermes", "whispercpp", "qwen3asr_server")
TTS_ENGINES = ("piper",)

# Repo-relative defaults (dev checkout: ``<repo>/packages/hermes-omni/...``).
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PIPER_BINARY = str(_REPO_ROOT / "gpu" / "tools" / "piper" / "piper")
DEFAULT_PIPER_MODEL = str(_REPO_ROOT / "models" / "en_US-lessac-medium.onnx")
DEFAULT_STT_BINARY = str(_REPO_ROOT / "gpu" / "tools" / "whisper-bin-ubuntu-x64" / "whisper-cli")
DEFAULT_STT_MODEL_PATH = str(_REPO_ROOT / "models" / "ggml-tiny.en.bin")
#: Warm GPU llama-server (Qwen3-ASR) used by ``stt.engine: qwen3asr_server``.
DEFAULT_STT_SERVER_URL = "http://127.0.0.1:8105"

#: Default voice-mode preamble (``voice.input_prompt``).  Attached as an ephemeral per-turn
#: prompt for turns in a channel bound to a live voice session, so the model knows the user
#: is SPEAKING and its reply will be SPOKEN aloud via TTS (and posted as a text transcript).
#: Kept short — it brands every turn's context.
DEFAULT_VOICE_INPUT_PROMPT = (
    "[Live voice call] The user is in a voice channel with you and is SPEAKING - their "
    "speech reaches you as an automatic transcript, so expect recognition noise, homophones "
    "and occasional blank/music-artifact lines; do not read those literally.\n"
    "Your reply WILL BE SPOKEN ALOUD via text-to-speech (and posted as the text transcript). "
    "Write for the ear: 1-3 short conversational sentences, no markdown, no lists, no code, "
    "no URLs. Never tell the user to type something and never say you posted or sent text - "
    "just answer out loud as in a phone call."
)

_TRUTHY = {"1", "true", "yes", "on"}


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _as_int(value: Any, *, default: int, lo: Optional[int] = None, hi: Optional[int] = None,
            key: str = "") -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        if value is not None:
            log.warning("hermes-omni: %signoring non-numeric value %r (using %s)",
                        f"{key}: " if key else "", value, default)
        return default
    if lo is not None:
        parsed = max(parsed, lo)
    if hi is not None:
        parsed = min(parsed, hi)
    return parsed


def _as_float(value: Any, *, default: float, lo: Optional[float] = None,
              hi: Optional[float] = None, key: str = "") -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        if value is not None:
            log.warning("hermes-omni: %signoring non-numeric value %r (using %s)",
                        f"{key}: " if key else "", value, default)
        return default
    if lo is not None:
        parsed = max(parsed, lo)
    if hi is not None:
        parsed = min(parsed, hi)
    return parsed


@dataclass
class VoiceConfig:
    """Parsed voice configuration block.  Defaults are the documented platform defaults."""

    enabled: bool = True
    #: Hard allow-list of voice channel ids.  Empty = explicit joins unrestricted.
    channels: Set[str] = field(default_factory=set)
    #: ``[(guild_id, channel_id), ...]`` joined automatically after gateway connect.
    auto_channels: List[Tuple[str, str]] = field(default_factory=list)
    #: Default guild for bare channel ids in ``auto_channels``.
    guild_id: Optional[str] = None
    #: ephemeral | channel | invoke
    session_binding: str = "channel"
    #: off | channel  (channel = post STT text + reply text into the bound channel)
    transcripts: str = "off"
    stt_engine: str = "hermes"          # hermes | whispercpp | qwen3asr_server
    stt_model: Optional[str] = None     # faster-whisper size for the hermes engine
    stt_threads: int = 2
    stt_language: str = "en"
    stt_binary: str = DEFAULT_STT_BINARY
    stt_model_path: str = DEFAULT_STT_MODEL_PATH
    #: ``stt.engine: qwen3asr_server`` — warm llama-server HTTP endpoint.
    stt_server_url: str = DEFAULT_STT_SERVER_URL
    #: Engine used when ``qwen3asr_server`` fails/degrades ("" disables the fallback).
    stt_fallback_engine: str = "whispercpp"
    #: Per-call budget for the ASR server request (thread-pool side; keep it warm-fast).
    stt_timeout_s: float = 60.0
    #: Ephemeral voice-mode preamble for turns in a voice-bound channel ("" disables).
    input_prompt: str = DEFAULT_VOICE_INPUT_PROMPT
    #: Single-slot queueing: a follow-up utterance/speech arriving while the previous one is
    #: still processing/playing is queued (latest wins for utterances) instead of dropped.
    queue_utterance: bool = True
    tts_engine: str = "piper"
    piper_binary: str = DEFAULT_PIPER_BINARY
    piper_model: str = DEFAULT_PIPER_MODEL
    tts_voice: Optional[str] = None     # reserved for engines with a voice knob
    silence_ms: int = 900
    min_utterance_ms: int = 300
    max_utterance_s: float = 30.0
    energy_threshold: float = 400.0
    auto_leave_after_s: int = 0         # 0 = off
    rejoin_max_attempts: int = 3
    join_timeout_s: float = 20.0
    speak_timeout_s: float = 90.0
    speak_dedupe_s: float = 30.0
    #: Platform adapter instance (None for standalone / test mode).
    platform_adapter: Any = None

    def auto_join_allowed(self, channel_id: str) -> bool:
        """Auto-channels must additionally pass the ``channels`` allow-list when set."""
        return not self.channels or str(channel_id) in self.channels


def _parse_auto_channels(raw: Any, default_guild: Optional[str]) -> List[Tuple[str, str]]:
    """Accept ``"guild:channel"``, ``{guild_id, channel_id}`` and bare channel ids."""
    out: List[Tuple[str, str]] = []
    if raw in (None, "", []):
        return out
    if isinstance(raw, dict):
        entries: List[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        entries = list(raw)
    elif isinstance(raw, str):
        entries = [part for part in re.split(r"[;,\s]+", raw) if part]
    else:
        log.warning("hermes-omni: auto_channels must be a list/dict/string, got %s — ignored",
                    type(raw).__name__)
        return out
    for entry in entries:
        guild: Any = None
        channel: Any = None
        if isinstance(entry, dict):
            guild = entry.get("guild_id") or entry.get("guild")
            channel = entry.get("channel_id") or entry.get("channel")
        elif isinstance(entry, str):
            text = entry.strip()
            if ":" in text or "/" in text:
                guild, channel = re.split(r"[:/]", text, maxsplit=1)
            else:
                channel = text
        guild_id = str(guild or default_guild or "").strip()
        channel_id = str(channel or "").strip()
        if not channel_id:
            log.warning("hermes-omni: auto_channels entry %r has no channel id — ignored", entry)
            continue
        if not guild_id:
            log.warning(
                "hermes-omni: auto_channels entry %r has no guild id (set voice.guild_id or "
                "'guild:channel' form) — ignored", entry)
            continue
        out.append((guild_id, channel_id))
    return out


def parse_voice_config(raw: Any, *, fallback_guild: Optional[str] = None) -> VoiceConfig:
    """Voice configuration → :class:`VoiceConfig`; never raises."""
    cfg = VoiceConfig()
    if raw is None:
        return cfg
    if not isinstance(raw, dict):
        log.warning("hermes-omni: voice config must be a mapping, got %s — defaults used",
                    type(raw).__name__)
        return cfg

    cfg.enabled = _as_bool(raw.get("enabled"), default=True)

    channels = raw.get("channels")
    if isinstance(channels, str):
        channels = re.split(r"[;,\s]+", channels)
    if isinstance(channels, (list, tuple, set)):
        cfg.channels = {str(part).strip() for part in channels if str(part).strip()}
    elif channels not in (None, ""):
        log.warning("hermes-omni: channels must be a list/comma string, got %r — ignored", channels)

    cfg.guild_id = str(raw.get("guild_id") or fallback_guild or "").strip() or None
    cfg.auto_channels = _parse_auto_channels(raw.get("auto_channels"), cfg.guild_id)

    binding = str(raw.get("session_binding") or "channel").strip().lower()
    if binding not in BINDING_MODES:
        log.warning("hermes-omni: unknown session_binding %r (expected %s) — using 'channel'",
                    raw.get("session_binding"), "|".join(BINDING_MODES))
        binding = "channel"
    cfg.session_binding = binding

    transcripts = str(raw.get("transcripts") or "off").strip().lower()
    if transcripts in ("on", "true", "1", "yes"):  # tolerate shorthand
        transcripts = "channel"
    if transcripts not in TRANSCRIPT_MODES:
        log.warning("hermes-omni: unknown transcripts mode %r (expected off|channel) — using 'off'",
                    raw.get("transcripts"))
        transcripts = "off"
    cfg.transcripts = transcripts

    stt = raw.get("stt")
    if isinstance(stt, dict):
        engine = str(stt.get("engine") or "hermes").strip().lower()
        if engine not in STT_ENGINES:
            log.warning("hermes-omni: unknown stt.engine %r (expected %s) — using 'hermes'",
                        stt.get("engine"), "|".join(STT_ENGINES))
            engine = "hermes"
        cfg.stt_engine = engine
        cfg.stt_model = str(stt.get("model") or "").strip() or None
        cfg.stt_threads = _as_int(stt.get("threads", 2), default=2, lo=1, hi=8, key="stt.threads")
        cfg.stt_language = str(stt.get("language") or "en")
        cfg.stt_binary = str(stt.get("binary") or cfg.stt_binary)
        cfg.stt_model_path = str(stt.get("model_path") or cfg.stt_model_path)
        cfg.stt_server_url = str(stt.get("server_url") or cfg.stt_server_url).strip()
        cfg.stt_timeout_s = _as_float(stt.get("timeout_s", 60.0), default=60.0, lo=5.0, hi=300.0,
                                      key="stt.timeout_s")
        raw_fb = stt.get("fallback_engine")
        if raw_fb is not None and str(raw_fb).strip() == "":
            # Explicit empty string → no fallback.
            cfg.stt_fallback_engine = ""
        else:
            fallback = str(raw_fb or "whispercpp").strip().lower()
            if fallback not in STT_ENGINES:
                log.warning("hermes-omni: unknown stt.fallback_engine %r (expected %s) — using 'whispercpp'",
                            stt.get("fallback_engine"), "|".join(STT_ENGINES))
                fallback = "whispercpp"
            if fallback == engine:
                log.warning("hermes-omni: stt.fallback_engine %r equals stt.engine — fallback disabled",
                            fallback)
                fallback = ""
            cfg.stt_fallback_engine = fallback
    elif stt is not None:
        log.warning("hermes-omni: stt must be a mapping, got %s — defaults used", type(stt).__name__)

    prompt = raw.get("input_prompt")
    if isinstance(prompt, str):
        cfg.input_prompt = prompt.strip()
    elif prompt is not None:
        log.warning("hermes-omni: input_prompt must be a string, got %s — default used",
                    type(prompt).__name__)
    cfg.queue_utterance = _as_bool(raw.get("queue_utterance"), default=True)

    tts = raw.get("tts")
    if isinstance(tts, dict):
        engine = str(tts.get("engine") or "piper").strip().lower()
        if engine not in TTS_ENGINES:
            log.warning("hermes-omni: unknown tts.engine %r (expected %s) — using 'piper'",
                        tts.get("engine"), "|".join(TTS_ENGINES))
            engine = "piper"
        cfg.tts_engine = engine
        cfg.piper_binary = str(tts.get("binary") or cfg.piper_binary)
        cfg.piper_model = str(tts.get("model") or cfg.piper_model)
        cfg.tts_voice = str(tts.get("voice") or "").strip() or None
    elif tts is not None:
        log.warning("hermes-omni: tts must be a mapping, got %s — defaults used", type(tts).__name__)

    cfg.silence_ms = _as_int(raw.get("silence_ms", 900), default=900, lo=100, hi=10_000,
                             key="silence_ms")
    cfg.min_utterance_ms = _as_int(raw.get("min_utterance_ms", 300), default=300, lo=20, hi=10_000,
                                   key="min_utterance_ms")
    cfg.max_utterance_s = _as_float(raw.get("max_utterance_s", 30.0), default=30.0, lo=1.0,
                                    hi=300.0, key="max_utterance_s")
    cfg.energy_threshold = _as_float(raw.get("energy_threshold", 400.0), default=400.0,
                                     lo=1.0, hi=30_000.0, key="energy_threshold")
    cfg.auto_leave_after_s = _as_int(raw.get("auto_leave_after_s", 0), default=0, lo=0,
                                     hi=86_400, key="auto_leave_after_s")
    cfg.rejoin_max_attempts = _as_int(raw.get("rejoin_max_attempts", 3), default=3, lo=0, hi=20,
                                      key="rejoin_max_attempts")
    cfg.join_timeout_s = _as_float(raw.get("join_timeout_s", 20.0), default=20.0, lo=2.0,
                                   hi=120.0, key="join_timeout_s")
    cfg.speak_timeout_s = _as_float(raw.get("speak_timeout_s", 90.0), default=90.0, lo=5.0,
                                    hi=600.0, key="speak_timeout_s")
    cfg.speak_dedupe_s = _as_float(raw.get("speak_dedupe_s", 30.0), default=30.0, lo=0.0,
                                   hi=600.0, key="speak_dedupe_s")
    return cfg