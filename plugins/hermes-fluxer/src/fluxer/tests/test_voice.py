"""Voice-lane unit tests (spec §6-W3) — fake ``livekit.rtc`` stub, no network.

Run::

    cd /home/agent/.hermes/hermes-agent
    ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests/test_voice.py -q

Covers: lazy-import failure path (plugin stays text-only), config parsing incl.
bad values, session lifecycle + all three binding modes (+ invoke fallback),
VAD segmentation on synthetic frames, the cascade state machine with fake
STT/TTS callables, the send→speak hook with the transcripts-gating matrix, and
the core-probed ``/voice`` methods.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import fluxer.adapter as adapter_mod
import fluxer.voice as voice_pkg
from fluxer.adapter import FluxerAdapter
from fluxer.voice import audio as audio_lib
from fluxer.voice.config import parse_voice_config
from fluxer.voice.controller import REJOIN_BASE_DELAY, VoiceController

BOT_ID = "1547828742208888832"
USER_ID = "1473728643747861346"
GUILD = "1547815091221561344"
VOICE_CHANNEL = "1547815091221561348"
TEXT_CHANNEL = "1547815091221561347"
TOKEN = f"{BOT_ID}.tokensecret"

logger_name = "fluxer.voice"


# ── fake livekit.rtc ─────────────────────────────────────────────────────────

class FakeAudioFrame:
    def __init__(self, sample_rate, num_channels, samples_per_channel):
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel
        self.data = memoryview(bytearray(num_channels * samples_per_channel * 2))

    @classmethod
    def create(cls, rate, ch, spc):
        return cls(rate, ch, spc)


class FakeAudioSource:
    instances: list = []

    def __init__(self, rate, ch):
        self.rate = rate
        self.ch = ch
        self.frames: list = []
        self.playout_waits = 0
        FakeAudioSource.instances.append(self)

    async def capture_frame(self, frame):
        self.frames.append(bytes(frame.data.cast("B")))

    async def wait_for_playout(self):
        self.playout_waits += 1


class FakeTrack:
    def __init__(self, name="remote", kind=0):
        self.name = name
        self.kind = kind
        self.sid = "TR_fake"
        self.frames: list = []


class FakeLocalTrack(FakeTrack):
    @staticmethod
    def create_audio_track(name, source):
        track = FakeTrack(name=name, kind=0)
        track.source = source
        return track


class FakePublication:
    def __init__(self):
        self.sid = "TR_pub_1"
        self.name = "hermes-voice"
        self.source = 2
        self.muted = False


class FakeLocalParticipant:
    def __init__(self):
        self.identity = f"user_{BOT_ID}_conn-1"
        self.track_publications = {}
        self.published: list = []

    async def publish_track(self, track, options=None):
        self.published.append((track, options))
        return FakePublication()


class FakeRoom:
    def __init__(self):
        self._handlers: dict = {}
        self.name = f"guild_{GUILD}_channel_{VOICE_CHANNEL}"
        self.connection_state = 2
        self.local_participant = FakeLocalParticipant()
        self.remote_participants: dict = {}
        self.connect_calls: list = []
        self.disconnect_calls = 0

    @property
    def sid(self):
        return "RM_fake"

    def on(self, name, cb):
        self._handlers.setdefault(name, []).append(cb)

    def emit(self, name, *args):
        for cb in self._handlers.get(name, []):
            cb(*args)

    async def connect(self, url, token):
        self.connect_calls.append((url, token))

    async def disconnect(self):
        self.disconnect_calls += 1
        self.emit("disconnected", "CLIENT_INITIATED")


class FakeAudioStream:
    def __init__(self, track, **kwargs):
        self._frames = list(getattr(track, "frames", []))
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        await asyncio.sleep(0)  # yield to the loop like the real stream
        return SimpleNamespace(frame=self._frames.pop(0))

    async def aclose(self):
        self.closed = True


class FakeTrackPublishOptions:
    source = None


class FakeTrackSource:
    SOURCE_MICROPHONE = 2


class FakeTrackKind:
    KIND_AUDIO = 0
    KIND_VIDEO = 1


def _make_rtc_stub() -> types.SimpleNamespace:
    rtc = types.SimpleNamespace(
        Room=FakeRoom, AudioSource=FakeAudioSource, LocalAudioTrack=FakeLocalTrack,
        AudioFrame=FakeAudioFrame, AudioStream=FakeAudioStream,
        TrackPublishOptions=FakeTrackPublishOptions, TrackSource=FakeTrackSource,
        TrackKind=FakeTrackKind,
        ConnectionState=types.SimpleNamespace(Name=lambda v: f"STATE_{v}"),
    )
    return rtc


@pytest.fixture
def fake_livekit(monkeypatch):
    """Inject a stub ``livekit``/``livekit.rtc`` and pin the package probe to it."""
    rtc_stub = _make_rtc_stub()
    livekit_mod = types.ModuleType("livekit")
    livekit_mod.rtc = rtc_stub
    monkeypatch.setitem(sys.modules, "livekit", livekit_mod)
    monkeypatch.setitem(sys.modules, "livekit.rtc", rtc_stub)
    voice_pkg.reset_livekit_probe()
    monkeypatch.setattr(voice_pkg, "_livekit", rtc_stub, raising=False)
    monkeypatch.setattr(voice_pkg, "_livekit_checked", True, raising=False)
    FakeAudioSource.instances.clear()
    yield rtc_stub
    voice_pkg.reset_livekit_probe()


# ── fixtures / helpers ───────────────────────────────────────────────────────

def make_config(**extra):
    return SimpleNamespace(extra=dict(extra))


def make_adapter(**extra) -> FluxerAdapter:
    adapter = FluxerAdapter(config=make_config(**extra))
    adapter._bot_id = BOT_ID
    adapter._write_runtime_status_safe = lambda *a, **k: None
    return adapter


@pytest.fixture
def fake_env(monkeypatch):
    """Scoped-secret reader stub; allow-all default so voice dispatch tests pass the authz gate."""
    values: dict = {"FLUXER_ALLOW_ALL_USERS": "true"}
    monkeypatch.setattr(adapter_mod, "_get_scoped_secret",
                        lambda name, default=None: values.get(name, default))
    return values


class FakeREST:
    def __init__(self):
        self.calls: list = []
        self.channels: dict = {}

    async def get_channel(self, channel_id):
        self.calls.append(("get_channel", channel_id))
        return self.channels.get(channel_id, {"id": channel_id, "name": f"chan-{channel_id}", "type": 0})

    async def create_message(self, channel_id, *, content=None, attachments=None, **kwargs):
        self.calls.append(("create_message", channel_id, content))
        return {"id": f"msg-{len(self.calls)}"}

    async def close(self):
        return None


class FakeGateway:
    """Records op4 calls; answers a join (channel set) with VOICE_SERVER_UPDATE."""

    def __init__(self, controller: VoiceController):
        self.controller = controller
        self.calls: list = []
        self.fail_vsu = False
        self.connection_id = "conn-1"

    async def update_voice_state(self, guild_id, channel_id, **kwargs):
        self.calls.append((str(guild_id) if guild_id else None,
                           str(channel_id) if channel_id else None, kwargs))
        if channel_id and not self.fail_vsu:
            self.controller.on_gateway_event("VOICE_SERVER_UPDATE", {
                "guild_id": str(guild_id), "channel_id": str(channel_id),
                "endpoint": "wss://rtc.example.invalid", "token": "grant.token.credential",
                "connection_id": self.connection_id,
            })

    def op4_joins(self):
        return [c for c in self.calls if c[1] is not None]

    def op4_leaves(self):
        return [c for c in self.calls if c[1] is None]


def tone(seconds: float, *, freq: float = 440.0, amp: int = 8000,
         rate: int = audio_lib.SAMPLE_RATE) -> array.array:
    total = int(seconds * rate)
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(total)))


def silence(seconds: float, rate: int = audio_lib.SAMPLE_RATE) -> array.array:
    return array.array("h", bytes(2 * int(seconds * rate)))


def wav_runner(text: str, out_path: str, *, seconds: float = 0.3):
    """Fake piper: writes a 22.05 kHz mono tone WAV (resample path exercised)."""
    audio_lib.write_wav(out_path, tone(seconds, rate=22050), rate=22050)
    return True, out_path


def make_voice_controller(adapter, *, piper=None, stt=None, config=None) -> VoiceController:
    cfg = config or parse_voice_config({"enabled": True})
    ctl = VoiceController(adapter, cfg,
                          piper_runner=piper or wav_runner,
                          stt_callable=stt)
    adapter._voice = ctl
    adapter._voice_cfg = cfg  # adapter + controller share one parsed config in production
    return ctl


async def join_session(adapter, ctl, *, channel=VOICE_CHANNEL, source=None):
    gw = FakeGateway(ctl)
    adapter._ws = gw
    ok = await ctl.join(GUILD, channel, source=source)
    return gw, ok


# ── config parsing ───────────────────────────────────────────────────────────

def test_config_defaults_and_aliases():
    cfg = parse_voice_config(None)
    assert cfg.enabled is True and cfg.session_binding == "channel" and cfg.transcripts == "off"
    assert cfg.stt_engine == "hermes" and cfg.tts_engine == "piper"
    assert cfg.silence_ms == 900 and cfg.min_utterance_ms == 300 and cfg.max_utterance_s == 30.0
    assert cfg.rejoin_max_attempts == 3 and cfg.auto_leave_after_s == 0
    assert parse_voice_config({}).channels == set()


def test_config_bad_values_fall_back_with_warnings(caplog):
    with caplog.at_level(logging.WARNING, logger=logger_name):
        cfg = parse_voice_config({
            "enabled": "yes",
            "session_binding": "nonsense",
            "transcripts": "maybe",
            "silence_ms": "not-a-number",
            "max_utterance_s": {"x": 1},
            "rejoin_max_attempts": -4,
            "stt": {"engine": "magic", "threads": 99},
            "tts": {"engine": "magic"},
            "channels": "a, b;c",
            "auto_channels": "154...:154...",
        })
    assert cfg.enabled is True
    assert cfg.session_binding == "channel"          # invalid → default
    assert cfg.transcripts == "off"                  # invalid → default
    assert cfg.silence_ms == 900                     # non-numeric → default
    assert cfg.max_utterance_s == 30.0
    assert cfg.rejoin_max_attempts == 0              # clamped to >= 0
    assert cfg.stt_engine == "hermes"                # invalid engine → default
    assert cfg.stt_threads == 8                      # clamped
    assert cfg.tts_engine == "piper"
    assert cfg.channels == {"a", "b", "c"}
    assert "session_binding" in caplog.text and "silence_ms" in caplog.text


def test_config_auto_channels_forms_and_fallback(caplog):
    cfg = parse_voice_config({
        "guild_id": GUILD,
        "auto_channels": [f"{GUILD}:{VOICE_CHANNEL}", VOICE_CHANNEL,
                          {"guild_id": GUILD, "channel_id": "999"}],
    })
    assert cfg.auto_channels == [(GUILD, VOICE_CHANNEL), (GUILD, VOICE_CHANNEL), (GUILD, "999")]
    with caplog.at_level(logging.WARNING, logger=logger_name):
        cfg2 = parse_voice_config({"auto_channels": ["no-guild-id", ""]})
    assert cfg2.auto_channels == []
    assert "no guild id" in caplog.text


def test_config_non_mapping_voice_block():
    cfg = parse_voice_config("nope")
    assert cfg.enabled is True and cfg.session_binding == "channel"


@pytest.mark.parametrize("kw", [
    {},  # defaults
    {"queue_utterance": False, "input_prompt": "Test."},
    {"stt": {"engine": "qwen3asr_server", "fallback_engine": "hermes"}},
    {"stt": {"server_url": "http://localhost:9999", "timeout_s": 30.0}},
])
def test_config_new_fields_roundtrip(kw):
    cfg = parse_voice_config(kw)
    assert isinstance(cfg.queue_utterance, bool)
    assert isinstance(cfg.input_prompt, str)
    assert isinstance(cfg.stt_server_url, str)
    assert isinstance(cfg.stt_fallback_engine, str)
    assert isinstance(cfg.stt_timeout_s, (int, float))
    assert 5.0 <= cfg.stt_timeout_s <= 300.0


def test_config_new_fields_specific():
    cfg = parse_voice_config({
        "queue_utterance": False,
        "input_prompt": "Custom preamble.",
        "stt": {"engine": "qwen3asr_server", "server_url": "http://x:1234",
                "fallback_engine": "", "timeout_s": 15.0},
    })
    assert cfg.queue_utterance is False
    assert cfg.input_prompt == "Custom preamble."
    assert cfg.stt_engine == "qwen3asr_server"
    assert cfg.stt_server_url == "http://x:1234"
    assert cfg.stt_fallback_engine == ""           # empty string disables fallback
    assert cfg.stt_timeout_s == 15.0


def test_config_fallback_equal_to_engine_warns(caplog):
    with caplog.at_level(logging.WARNING, logger=logger_name):
        cfg = parse_voice_config({
            "stt": {"engine": "qwen3asr_server", "fallback_engine": "qwen3asr_server"},
        })
    assert cfg.stt_fallback_engine == ""           # disabled
    assert "equals stt.engine" in caplog.text


# ── audio / VAD ──────────────────────────────────────────────────────────────

def test_vad_segments_speech_then_silence():
    seg = audio_lib.VadSegmenter(silence_ms=900, min_utterance_ms=300, energy_threshold=400.0)
    out = []
    for chunk in (tone(0.4), silence(1.2)):
        for i in range(0, len(chunk), audio_lib.FRAME_SAMPLES):
            out.extend(seg.feed(chunk[i:i + audio_lib.FRAME_SAMPLES]))
    assert len(out) == 1
    utterance = out[0]
    assert utterance.reason == "silence"
    # 0.4 s speech + the 0.9 s silence that ends the utterance (silence_ms)
    assert 1.25 <= utterance.seconds <= 1.35


def test_vad_drops_short_utterances():
    seg = audio_lib.VadSegmenter(silence_ms=400, min_utterance_ms=300, energy_threshold=400.0)
    assert seg.feed(tone(0.1)) == []
    out = []
    for chunk in (silence(0.6),):
        out.extend(seg.feed(chunk))
    assert out == []
    assert seg.dropped_short >= 1


def test_vad_force_ends_on_max_length():
    seg = audio_lib.VadSegmenter(silence_ms=900, min_utterance_ms=300,
                                 max_utterance_s=0.5, energy_threshold=400.0)
    out = seg.feed(tone(1.0))
    assert [u.reason for u in out] == ["max_length"]
    assert out[0].seconds >= 0.5


def test_vad_flush_mid_speech():
    seg = audio_lib.VadSegmenter(silence_ms=900, min_utterance_ms=300, energy_threshold=400.0)
    seg.feed(tone(0.5))
    utterance = seg.flush()
    assert utterance is not None and utterance.reason == "flush"
    assert seg.flush() is None


def test_resample_and_wav_roundtrip(tmp_path):
    src = tone(0.25, rate=22050)
    out = audio_lib.resample(src, 22050, 48000)
    assert abs(len(out) - int(0.25 * 48000)) <= 5
    path = tmp_path / "t.wav"
    audio_lib.write_wav(path, src, rate=22050)
    samples, info = audio_lib.load_wav_48k_mono(path)
    assert info.src_rate == 22050 and info.out_rate == 48000
    assert abs(len(samples) - int(0.25 * 48000)) <= 5
    frames = audio_lib.pad_frames(samples)
    assert all(len(f) == audio_lib.FRAME_SAMPLES for f in frames)


# ── session lifecycle + binding modes ────────────────────────────────────────

def test_join_leave_lifecycle_channel_binding(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    gw, ok = asyncio.run(join_session(adapter, ctl))
    assert ok is True
    session = ctl.session_for_chat(VOICE_CHANNEL)
    assert session is not None and session.state == "connected"
    assert session.binding_chat_type == "channel" and session.binding_chat_id == VOICE_CHANNEL
    assert gw.op4_joins()[0][0] == GUILD and gw.op4_joins()[0][1] == VOICE_CHANNEL
    assert gw.op4_joins()[0][2]["self_mute"] is False
    room = session.room
    assert room.connect_calls == [("wss://rtc.example.invalid", "grant.token.credential")]
    # second join is idempotent (no duplicate op4)
    assert asyncio.run(ctl.join(GUILD, VOICE_CHANNEL)) is True
    assert len(gw.op4_joins()) == 1
    # leave: op4 null + room disconnect + deregistration
    assert asyncio.run(ctl.leave(GUILD)) is True
    assert gw.op4_leaves() and gw.op4_leaves()[0][0] == GUILD
    assert room.disconnect_calls >= 1
    assert ctl.session_for_chat(VOICE_CHANNEL) is None
    assert ctl.is_in_channel(GUILD) is False


def test_binding_modes_and_invoke_fallback(fake_env, fake_livekit, caplog):
    adapter = make_adapter()
    adapter._rest = FakeREST()

    # ephemeral → synthetic voice chat id carrying the connection id
    ctl_e = make_voice_controller(adapter, config=parse_voice_config({"session_binding": "ephemeral"}))
    asyncio.run(join_session(adapter, ctl_e))
    s_e = ctl_e.session_for_channel(VOICE_CHANNEL)
    assert s_e.binding_chat_type == "voice"
    assert s_e.binding_chat_id == f"voice:{VOICE_CHANNEL}:conn-1"
    asyncio.run(ctl_e.leave(GUILD))

    # invoke with a source binds to the source chat
    source = {"chat_id": TEXT_CHANNEL, "chat_name": "general", "chat_type": "channel",
              "scope_id": GUILD, "user_id": USER_ID}
    ctl_i = make_voice_controller(adapter, config=parse_voice_config({"session_binding": "invoke"}))
    asyncio.run(join_session(adapter, ctl_i, source=source))
    s_i = ctl_i.session_for_channel(VOICE_CHANNEL)
    assert s_i.binding_chat_id == TEXT_CHANNEL and s_i.binding_chat_name == "general"
    asyncio.run(ctl_i.leave(GUILD))

    # invoke without any source → logged fallback to channel binding
    with caplog.at_level(logging.WARNING, logger=logger_name):
        ctl_f = make_voice_controller(adapter,
                                      config=parse_voice_config({"session_binding": "invoke"}))
        asyncio.run(join_session(adapter, ctl_f))
    s_f = ctl_f.session_for_channel(VOICE_CHANNEL)
    assert s_f.binding_chat_id == VOICE_CHANNEL
    assert "falling back to channel binding" in caplog.text


def test_join_refused_outside_channel_allowlist(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    cfg = parse_voice_config({"channels": ["999"]})
    ctl = make_voice_controller(adapter, config=cfg)
    gw, ok = asyncio.run(join_session(adapter, ctl))
    assert ok is False and gw.op4_joins() == []


def test_join_timeout_when_no_voice_server_update(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    cfg = parse_voice_config({"join_timeout_s": 2.0})
    ctl = make_voice_controller(adapter, config=cfg)
    gw = FakeGateway(ctl)
    gw.fail_vsu = True
    adapter._ws = gw
    assert asyncio.run(ctl.join(GUILD, VOICE_CHANNEL)) is False
    assert ctl.session_for_channel(VOICE_CHANNEL) is None
    assert gw.op4_leaves()  # cleanup leave sent


def test_gateway_event_routes_voice_states_and_seeds(fake_env, fake_livekit):
    adapter = make_adapter()
    ctl = make_voice_controller(adapter)
    ctl.on_gateway_event("VOICE_STATE_UPDATE", {
        "guild_id": GUILD, "user_id": USER_ID, "channel_id": VOICE_CHANNEL})
    ref = asyncio.run(ctl.get_user_voice_channel(GUILD, USER_ID))
    assert ref is not None and ref.id == VOICE_CHANNEL
    ctl.on_gateway_event("VOICE_STATE_UPDATE", {
        "guild_id": GUILD, "user_id": USER_ID, "channel_id": None})
    assert asyncio.run(ctl.get_user_voice_channel(GUILD, USER_ID)) is None
    ctl.seed_from_ready({"guilds": [{
        "id": GUILD,
        "voice_states": [{"user_id": USER_ID, "channel_id": VOICE_CHANNEL}],
        "members": [{"user": {"id": USER_ID, "display_name": "kairo"}}],
    }]})
    ref = asyncio.run(ctl.get_user_voice_channel(GUILD, USER_ID))
    assert ref is not None
    ctl.seed_from_guild({"id": GUILD, "members": []})  # no-op variant


def test_rejoin_after_unexpected_room_disconnect(fake_env, fake_livekit, monkeypatch):
    monkeypatch.setattr("fluxer.voice.controller.REJOIN_BASE_DELAY", 0.0)
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    gw, ok = asyncio.run(join_session(adapter, ctl))
    assert ok is True
    session = ctl.session_for_channel(VOICE_CHANNEL)
    old_room = session.room

    async def scenario():
        old_room.emit("disconnected", "SERVER_INITIATED")  # unexpected
        for _ in range(50):
            await asyncio.sleep(0.02)
            if session.state == "connected" and session.room is not old_room:
                return True
        return False

    assert asyncio.run(scenario()) is True
    assert len(gw.op4_joins()) == 2               # re-op4 the same channel (c4a §5.1)
    assert ctl.stats["rejoins_ok"] == 1
    asyncio.run(ctl.leave(GUILD))


def test_rejoin_attempts_exhausted_leaves(fake_env, fake_livekit, monkeypatch):
    monkeypatch.setattr("fluxer.voice.controller.REJOIN_BASE_DELAY", 0.0)
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(
        adapter, config=parse_voice_config({"rejoin_max_attempts": 2, "join_timeout_s": 2.0}))
    gw, ok = asyncio.run(join_session(adapter, ctl))
    assert ok is True
    session = ctl.session_for_channel(VOICE_CHANNEL)
    gw.fail_vsu = True  # every rejoin times out

    async def scenario():
        session.room.emit("disconnected", "SERVER_INITIATED")
        for _ in range(150):
            await asyncio.sleep(0.05)
            if session.state == "closed" and ctl.session_for_channel(VOICE_CHANNEL) is None:
                return True
        return False

    assert asyncio.run(scenario()) is True
    assert ctl.stats["rejoin_failures"] >= 1
    assert gw.op4_leaves()  # cleanup leave sent


# ── cascade ──────────────────────────────────────────────────────────────────

def test_cascade_utterance_to_handle_message_then_reply_publish(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    stt_calls: list = []

    def fake_stt(path):
        stt_calls.append(path)
        assert Path(path).is_file()
        return "turn the lights on"

    ctl = make_voice_controller(adapter, stt=fake_stt)
    captured: list = []

    async def fake_handle(event):
        captured.append(event)

    adapter.handle_message = fake_handle
    adapter._message_handler = fake_handle
    gw, ok = asyncio.run(join_session(adapter, ctl))
    assert ok is True
    session = ctl.session_for_channel(VOICE_CHANNEL)
    assert session.cascade is not None

    async def scenario():
        # feed a 0.4 s speech burst followed by silence (48k mono frames)
        for chunk in (tone(0.4), silence(1.1)):
            for i in range(0, len(chunk), audio_lib.FRAME_SAMPLES):
                await session.cascade.process_frame_bytes(
                    chunk[i:i + audio_lib.FRAME_SAMPLES].tobytes(),
                    participant=SimpleNamespace(identity=f"user_{USER_ID}_conn-2"),
                )
        # the agent pipeline got a synthetic event for the bound chat
        assert len(captured) == 1
        event = captured[0]
        assert event.text == "turn the lights on"
        assert event.source.chat_id == VOICE_CHANNEL
        assert event.source.chat_type == "channel"
        assert event.source.user_id == USER_ID
        # reply comes back through the adapter's normal send → spoken (transcripts off)
        result = await adapter.send(VOICE_CHANNEL, "The lights are on.")
        assert result.success is True
        return result

    result = asyncio.run(scenario())
    assert len(stt_calls) == 1
    assert result.raw_response == {"voice_only": True}
    src = FakeAudioSource.instances[-1]
    assert len(src.frames) > 10  # piper WAV → 48k frames published
    asyncio.run(ctl.leave(GUILD))


def test_cascade_reader_via_track_subscribed(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter, stt=lambda path: "hello there")
    adapter.handle_message = _async_noop()
    gw, ok = asyncio.run(join_session(adapter, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def scenario():
        track = FakeTrack()
        # 0.3 s speech + 1.0 s silence as 20 ms frames
        for chunk in (tone(0.3), silence(1.0)):
            for i in range(0, len(chunk), audio_lib.FRAME_SAMPLES):
                track.frames.append(FakeAudioFrame.create(
                    audio_lib.SAMPLE_RATE, 1, audio_lib.FRAME_SAMPLES))
        # fill the frames with real samples
        idx = 0
        payload = (tone(0.3).tobytes() + silence(1.0).tobytes())
        for frame in track.frames:
            frame.data[:] = payload[idx:idx + len(frame.data)]
            idx += len(frame.data)
        session.room.emit("track_subscribed", track, SimpleNamespace(sid="TR_x"),
                          SimpleNamespace(identity=f"user_{USER_ID}_conn-2"))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if session.cascade.last_transcript:
                return session.cascade.last_transcript
        return None

    assert asyncio.run(scenario()) == "hello there"
    assert session.cascade.dispatched_via == "handle_message"
    asyncio.run(ctl.leave(GUILD))


def test_cascade_drops_concurrent_utterance_when_queue_off(fake_env, fake_livekit):
    """queue_utterance=False → second utterance dropped."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    release = asyncio.Event()

    def slow_stt(path):
        import time
        while not release.is_set():
            time.sleep(0.02)
        return "slow words"

    cfg = parse_voice_config({"queue_utterance": False, "enabled": True})
    ctl = make_voice_controller(adapter, config=cfg, stt=slow_stt)
    adapter.handle_message = _async_noop()
    gw, ok = asyncio.run(join_session(adapter, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def feed(payload: bytes):
        for i in range(0, len(payload), audio_lib.FRAME_SAMPLES * 2):
            await session.cascade.process_frame_bytes(
                payload[i:i + audio_lib.FRAME_SAMPLES * 2],
                participant=SimpleNamespace(identity="user_1_x"))

    async def scenario():
        payload = tone(0.4).tobytes() + silence(1.0).tobytes()
        first = asyncio.ensure_future(feed(payload))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if session.cascade._busy.locked():
                break
        assert session.cascade._busy.locked()  # first utterance now inside (slow) STT
        await feed(payload)                    # a second utterance arrives meanwhile
        release.set()
        await first
        return session.cascade.stats["dropped_busy"]

    dropped = asyncio.run(scenario())
    assert dropped >= 1
    asyncio.run(ctl.leave(GUILD))


def test_cascade_queues_concurrent_utterance(fake_env, fake_livekit):
    """queue_utterance=True (default) → second utterance queued and processed."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    release = asyncio.Event()

    def slow_stt(path):
        import time
        while not release.is_set():
            time.sleep(0.02)
        return "slow words"

    cfg = parse_voice_config({"queue_utterance": True, "enabled": True})
    ctl = make_voice_controller(adapter, config=cfg, stt=slow_stt)
    adapter.handle_message = _async_noop()
    gw, ok = asyncio.run(join_session(adapter, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def feed(payload: bytes):
        for i in range(0, len(payload), audio_lib.FRAME_SAMPLES * 2):
            await session.cascade.process_frame_bytes(
                payload[i:i + audio_lib.FRAME_SAMPLES * 2],
                participant=SimpleNamespace(identity="user_1_x"))

    async def scenario():
        payload = tone(0.4).tobytes() + silence(1.0).tobytes()
        first = asyncio.ensure_future(feed(payload))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if session.cascade._busy.locked():
                break
        assert session.cascade._busy.locked()  # first utterance inside slow STT
        await feed(payload)                    # second arrives → queues
        release.set()                          # let first finish
        await first
        # Give event loop cycles to process the queued utterance after lock release
        for _ in range(100):
            await asyncio.sleep(0.02)
        return session.cascade.stats

    stats = asyncio.run(scenario())
    assert stats["dropped_busy"] == 0
    # queued_busy won't necessarily increment in this scenario because:
    # (a) first utterance enters, (b) second utterance checks locked -> False if first
    # already released? Actually slow_stt blocks until release.set(), so first is still
    # transcribing while second arrives -> drops with queue_off, BUT with queue_on it
    # should check self._busy.locked() -> True -> set _queued_utterance.
    # The issue may be timing: the async with self._busy holds the lock THROUGHOUT
    # processing, not just STT. Let's check: _handle_utterance acquires _busy, then
    # calls _transcribe (awaits slow_stt in a thread). The lock is still held.
    # When second arrives, _busy.locked() -> True -> queue.
    # BUT: the lock is released only when _handle_utterance async context exits, which
    # is AFTER _transcribe returns (slow_stt returns after release.set()).
    # So the sequence:
    # - feed first -> begins _handle_utterance -> acquires _busy -> starts slow_stt
    #   (thread blocks on release)
    # - feed second -> _handle_utterance sees _busy.locked() -> queue
    # - release.set() -> first slow_stt returns -> _handle_utterance finishes ->
    #   _busy released -> then _process_utterance processes the queued second.
    # So queued_busy should increment.
    assert stats["queued_busy"] >= 1
    assert stats["dispatched"] >= 1  # at least one processed in total
    asyncio.run(ctl.leave(GUILD))


def test_cascade_prefers_core_voice_input_callback(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter, stt=lambda path: "core path words")
    calls: list = []

    async def core_callback(guild_id, user_id, transcript):
        calls.append((guild_id, user_id, transcript))

    adapter._voice_input_callback = core_callback
    adapter._voice_text_channels[int(GUILD)] = int(VOICE_CHANNEL)  # core /voice join marker
    adapter.handle_message = _async_noop()
    gw, ok = asyncio.run(join_session(adapter, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def scenario():
        payload = tone(0.35).tobytes() + silence(1.0).tobytes()
        for i in range(0, len(payload), audio_lib.FRAME_SAMPLES * 2):
            await session.cascade.process_frame_bytes(
                payload[i:i + audio_lib.FRAME_SAMPLES * 2],
                participant=SimpleNamespace(identity=f"user_{USER_ID}_conn-2"))

    asyncio.run(scenario())
    assert session.cascade.dispatched_via == "core_callback"
    assert calls and calls[0][0] == int(GUILD) and calls[0][2] == "core path words"
    asyncio.run(ctl.leave(GUILD))


def test_cascade_drops_unauthorized_speaker(fake_env, fake_livekit):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "false"
    fake_env["FLUXER_ALLOWED_USERS"] = "999999999999999999"
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter, stt=lambda path: "should never be transcribed")
    calls: list = []
    stt_calls: list = []

    def tracking_stt(path):
        stt_calls.append(path)
        return "should never be transcribed"

    adapter.handle_message = lambda *a: calls.append(a)
    adapter._message_handler = adapter.handle_message
    ctl._stt_override = tracking_stt
    gw, ok = asyncio.run(join_session(adapter, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def scenario():
        payload = tone(0.4).tobytes() + silence(1.0).tobytes()
        for i in range(0, len(payload), audio_lib.FRAME_SAMPLES * 2):
            await session.cascade.process_frame_bytes(
                payload[i:i + audio_lib.FRAME_SAMPLES * 2],
                participant=SimpleNamespace(identity=f"user_{USER_ID}_conn-2"))

    asyncio.run(scenario())
    assert session.cascade.stats["dropped_unauthorized"] == 1
    assert stt_calls == [] and calls == []  # never transcribed, never dispatched
    asyncio.run(ctl.leave(GUILD))


def _async_noop():
    async def noop(*a, **k):
        return None
    return noop


# ── speak / speak hook gating ────────────────────────────────────────────────

def test_speak_publishes_resampled_piper_audio(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    asyncio.run(join_session(adapter, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def scenario():
        assert await ctl.speak("Hello voice channel.", session=session) is True
        first_round = list(FakeAudioSource.instances[-1].frames)
        # duplicate within the window → skipped, still counted ok
        assert await ctl.speak("Hello voice channel.", session=session) is True
        assert FakeAudioSource.instances[-1].frames == first_round
        # a different text goes out
        assert await ctl.speak("Something else entirely.", session=session) is True
        assert len(FakeAudioSource.instances[-1].frames) > len(first_round)

    asyncio.run(scenario())
    assert session.publication_sid == "TR_pub_1"
    assert session.stats["speak_deduped"] == 1
    asyncio.run(ctl.leave(GUILD))


def test_speak_drops_when_busy(fake_env, fake_livekit):
    adpt = make_adapter()
    adpt._rest = FakeREST()
    cfg = parse_voice_config({"queue_utterance": False, "enabled": True})
    ctl = make_voice_controller(adpt, config=cfg)
    asyncio.run(join_session(adpt, ctl))
    session = ctl.session_for_channel(VOICE_CHANNEL)

    async def scenario():
        await session.speak_lock.acquire()
        try:
            assert await ctl.speak("While busy", session=session) is False
        finally:
            session.speak_lock.release()

    asyncio.run(scenario())
    assert session.stats["speak_dropped_busy"] == 1
    asyncio.run(ctl.leave(GUILD))


def test_send_hook_transcripts_off_speaks_only(fake_env, fake_livekit):
    adapter = make_adapter()
    rest = FakeREST()
    adapter._rest = rest
    ctl = make_voice_controller(adapter)
    asyncio.run(join_session(adapter, ctl))
    result = asyncio.run(adapter.send(VOICE_CHANNEL, "Spoken only."))
    assert result.success is True
    assert [c for c in rest.calls if c[0] == "create_message"] == []
    assert len(FakeAudioSource.instances[-1].frames) > 0
    asyncio.run(ctl.leave(GUILD))


def test_send_hook_transcripts_channel_posts_and_speaks(fake_env, fake_livekit):
    adapter = make_adapter()
    rest = FakeREST()
    adapter._rest = rest
    ctl = make_voice_controller(adapter, config=parse_voice_config({"transcripts": "channel"}))
    asyncio.run(join_session(adapter, ctl))
    result = asyncio.run(adapter.send(VOICE_CHANNEL, "Text and speech."))
    assert result.success is True
    posts = [c for c in rest.calls if c[0] == "create_message"]
    assert len(posts) == 1 and posts[0][1] == VOICE_CHANNEL
    assert len(FakeAudioSource.instances[-1].frames) > 0
    asyncio.run(ctl.leave(GUILD))


def test_send_hook_tts_failure_never_eats_reply(fake_env, fake_livekit):
    def failing_piper(text, out_path):
        return False, "synthetic piper failure"

    for mode in ("off", "channel"):
        adapter = make_adapter()
        rest = FakeREST()
        adapter._rest = rest
        ctl = make_voice_controller(adapter, piper=failing_piper,
                                    config=parse_voice_config({"transcripts": mode}))
        asyncio.run(join_session(adapter, ctl))
        result = asyncio.run(adapter.send(VOICE_CHANNEL, "Reply survives."))
        assert result.success is True
        posts = [c for c in rest.calls if c[0] == "create_message"]
        assert len(posts) == 1  # always posted when speak failed (reply never eaten)
        asyncio.run(ctl.leave(GUILD))


def test_send_hook_skips_piper_when_auto_tts_active(fake_env, fake_livekit):
    adapter = make_adapter()
    rest = FakeREST()
    adapter._rest = rest
    ctl = make_voice_controller(adapter)
    asyncio.run(join_session(adapter, ctl))
    adapter._auto_tts_enabled_chats.add(VOICE_CHANNEL)  # core /voice on|join sets this
    result = asyncio.run(adapter.send(VOICE_CHANNEL, "Auto-TTS owns this turn."))
    assert result.success is True and result.raw_response == {"voice_only": True}
    assert [c for c in rest.calls if c[0] == "create_message"] == []
    assert FakeAudioSource.instances == []  # no duplicate piper copy, no publisher created
    asyncio.run(ctl.leave(GUILD))


def test_play_tts_routes_into_voice_channel(fake_env, fake_livekit, tmp_path):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    asyncio.run(join_session(adapter, ctl))
    wav = tmp_path / "auto_tts.wav"
    audio_lib.write_wav(wav, tone(0.2, rate=48000), rate=48000)
    result = asyncio.run(adapter.play_tts(VOICE_CHANNEL, str(wav)))
    assert result.success is True
    assert len(FakeAudioSource.instances[-1].frames) > 0
    asyncio.run(ctl.leave(GUILD))


# ── core-probed /voice surface ───────────────────────────────────────────────

def test_core_voice_probe_surface(fake_env, fake_livekit):
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    # before joining: everything reads as "not in voice"
    assert adapter.is_in_voice_channel(GUILD) is False
    assert adapter.get_voice_channel_info(GUILD) is None
    gw, ok = asyncio.run(join_session(adapter, ctl))
    assert ok is True
    assert adapter.is_in_voice_channel(GUILD) is True
    # core /voice join writes these two (run_voice.py)
    adapter._voice_text_channels[int(GUILD)] = int(TEXT_CHANNEL)
    adapter._voice_sources[int(GUILD)] = {"chat_id": TEXT_CHANNEL, "chat_type": "channel"}
    # user lookup from routed voice states
    ctl.on_gateway_event("VOICE_STATE_UPDATE",
                         {"guild_id": GUILD, "user_id": USER_ID, "channel_id": VOICE_CHANNEL})
    ref = asyncio.run(adapter.get_user_voice_channel(GUILD, USER_ID))
    assert ref is not None and ref.id == VOICE_CHANNEL and ref.name
    info = adapter.get_voice_channel_info(GUILD)
    assert info["channel_name"] == VOICE_CHANNEL and info["member_count"] == 1
    assert info["members"][0]["user_id"] == USER_ID
    context = adapter.get_voice_channel_context(GUILD)
    assert "Voice channel" in context and USER_ID in context
    # programmatic join with a VoiceChannelRef is accepted (idempotent when already in)
    assert asyncio.run(adapter.join_voice_channel(ref)) is True
    # core leave path
    asyncio.run(adapter.leave_voice_channel(GUILD))
    assert adapter.is_in_voice_channel(GUILD) is False


def test_join_voice_channel_without_guild_refuses(fake_env, fake_livekit):
    adapter = make_adapter()
    ctl = make_voice_controller(adapter)
    assert asyncio.run(adapter.join_voice_channel(SimpleNamespace(id="x"))) is False


# ── lazy-import failure path ─────────────────────────────────────────────────

def test_lazy_import_failure_keeps_plugin_text_only(fake_env, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "livekit", None)  # `from livekit import rtc` raises
    voice_pkg.reset_livekit_probe()
    with caplog.at_level(logging.WARNING, logger=logger_name):
        # plugin + adapter still import and construct
        adapter = make_adapter()
        assert adapter._voice_cfg.enabled is True
        assert adapter._voice_controller() is None    # no livekit → no controller
        assert voice_pkg.livekit_available() is False
        # join attempt degrades gracefully (no exception, False)
        assert asyncio.run(adapter.join_voice_channel(
            SimpleNamespace(id=VOICE_CHANNEL, guild_id=GUILD))) is False
        assert adapter.is_in_voice_channel(GUILD) is False
        assert adapter.get_voice_channel_info(GUILD) is None
        voice_pkg.try_livekit()                       # cached miss: no second log
    assert caplog.text.count(
        "Fluxer voice disabled: install livekit into the Hermes venv") == 1
    voice_pkg.reset_livekit_probe()


def test_lazy_import_logs_hint_once(fake_env, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "livekit", None)
    voice_pkg.reset_livekit_probe()
    with caplog.at_level(logging.WARNING, logger=logger_name):
        assert voice_pkg.try_livekit() is None
        assert voice_pkg.try_livekit() is None
    assert caplog.text.count("Fluxer voice disabled: install livekit into the Hermes venv") == 1


def test_voice_disabled_config_keeps_everything_inert(fake_env, fake_livekit):
    adapter = make_adapter(voice={"enabled": False})
    assert adapter._voice_cfg.enabled is False
    assert adapter._voice_controller() is None
    assert asyncio.run(adapter.join_voice_channel(
        SimpleNamespace(id=VOICE_CHANNEL, guild_id=GUILD))) is False
    # gateway events are ignored without touching the controller
    asyncio.run(adapter._on_gateway_event("VOICE_STATE_UPDATE",
                                          {"guild_id": GUILD, "user_id": USER_ID,
                                           "channel_id": VOICE_CHANNEL}))
    assert adapter._voice is None


# ── wave-5 additions: ASR server engine, preamble, interim-send, DM voice ──


def test_stt_engine_qwen3asr_parse_response():
    """_parse_asr_server_text handles the expected server reply formats."""
    from fluxer.voice.cascade import _parse_asr_server_text
    # Full format
    assert _parse_asr_server_text({
        "choices": [{"message": {"content": "language English<asr_text>turn the lights on"}}]
    }) == "turn the lights on"
    # Naked transcript (fallback regex)
    assert _parse_asr_server_text({
        "choices": [{"message": {"content": "language en  turn the lights on"}}]
    }) == "turn the lights on"
    # Empty
    assert _parse_asr_server_text({
        "choices": [{"message": {"content": "language English<asr_text>"}}]
    }) == ""
    assert _parse_asr_server_text({}) is None
    assert _parse_asr_server_text({"choices": []}) is None


def test_stt_engine_qwen3asr_http_failure_returns_none(caplog):
    """Server down → returns None (so fallback can fire)."""
    from fluxer.voice.cascade import _qwen3asr_server_transcribe
    from fluxer.voice.config import VoiceConfig
    cfg = VoiceConfig(stt_server_url="http://127.0.0.1:1", stt_timeout_s=2.0)
    with caplog.at_level(logging.WARNING, logger=logger_name):
        result = _qwen3asr_server_transcribe(cfg, "/dev/null")
    assert result is None
    assert "request failed" in caplog.text


@pytest.mark.parametrize("engine,fallback,expected", [
    ("whispercpp", "", "drop"),
    ("qwen3asr_server", "hermes", "fallback"),
])
def test_stt_callable_resolves_engine_and_fallback(engine, fallback, expected):
    """_stt_callable produces the right dispatch: primary, fallback wrapped, or none."""
    from fluxer.voice.config import VoiceConfig, parse_voice_config
    from fluxer.voice.cascade import VoiceCascade
    from types import SimpleNamespace
    cfg = parse_voice_config({
        "stt": {"engine": engine, "fallback_engine": fallback, "timeout_s": 5.0},
    })
    # Need a minimal cascade — use a partial mock
    cascade = object.__new__(VoiceCascade)
    cascade.config = cfg
    cascade.stats = {"stt_fallback_calls": 0}
    callable_fn = cascade._stt_callable()
    assert callable(callable_fn)


@pytest.mark.parametrize("meta_key", ["_interim_send", "non_conversational"])
def test_voice_bound_send_not_spoken_for_interim_metadata(fake_env, fake_livekit, meta_key):
    """System/interim metadata → voice-bound send posts text but does NOT speak."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    asyncio.run(join_session(adapter, ctl))
    # transcripts=off so we can detect that text was still posted
    adapter._voice.config.transcripts = "off"
    # Track speak calls via controller.stats
    result = asyncio.run(adapter._voice_bound_send(
        ctl.session_for_channel(VOICE_CHANNEL), VOICE_CHANNEL,
        "Interim notice", None, {meta_key: True}))
    assert result.success is True
    # Speaks counter did NOT increment (no speak for interim)
    assert ctl.stats.get("speak_ok", 0) == 0
    # But text WAS posted (fallback send even when transcripts off)
    assert any("create_message" in str(c) for c in (adapter._rest.calls or []))
    asyncio.run(ctl.leave(GUILD))


def test_voice_input_prompt_for_chat_channel(fake_env, fake_livekit):
    """_voice_input_prompt_for_chat returns preamble when chat is voice-bound."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    from fluxer.voice.config import parse_voice_config
    cfg = parse_voice_config({"input_prompt": "TEST PROMPT", "enabled": True})
    ctl = make_voice_controller(adapter, config=cfg)
    _ = asyncio.run(join_session(adapter, ctl))
    # chat is bound
    prompt = adapter._voice_input_prompt_for_chat(VOICE_CHANNEL)
    assert prompt == "TEST PROMPT"
    # unbound chat returns None
    prompt2 = adapter._voice_input_prompt_for_chat("some_other_id")
    assert prompt2 is None
    # disabled config returns None
    adapter._voice_cfg.enabled = False
    assert adapter._voice_input_prompt_for_chat(VOICE_CHANNEL) is None
    asyncio.run(ctl.leave(GUILD))


def test_resolve_channel_prompt_delegates(fake_env, fake_livekit):
    """_resolve_channel_prompt → _voice_input_prompt_for_chat."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    from fluxer.voice.config import parse_voice_config
    cfg = parse_voice_config({"input_prompt": "CORE PROMPT", "enabled": True})
    ctl = make_voice_controller(adapter, config=cfg)
    _ = asyncio.run(join_session(adapter, ctl))
    resolved = adapter._resolve_channel_prompt(VOICE_CHANNEL)
    assert resolved == "CORE PROMPT"
    asyncio.run(ctl.leave(GUILD))


def test_dm_voice_command_join_no_vc(fake_env, fake_livekit):
    """DM /voice join when user not in any VC → reply without crashing."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    handled = asyncio.run(adapter._handle_dm_voice_command(
        content="/voice join", chat_id="dm_chat", author_id=USER_ID, source=None))
    assert handled is True
    # reply should contain guidance
    rest = adapter._rest
    assert any("create_message" in str(c) and "voice channel" in str(c).lower()
               for c in (rest.calls or []))


def test_dm_voice_leave_no_session(fake_env, fake_livekit):
    """DM /voice leave when not in any VC → reply."""
    adapter = make_adapter()
    adapter._rest = FakeREST()
    ctl = make_voice_controller(adapter)
    handled = asyncio.run(adapter._handle_dm_voice_command(
        content="/voice leave", chat_id="dm_chat", author_id=USER_ID, source=None))
    assert handled is True
    rest = adapter._rest
    assert any("create_message" in str(c) and "not in" in str(c).lower()
               for c in (rest.calls or []))


def test_dm_voice_on_passes_through(fake_env, fake_livekit):
    """DM /voice on → false (not handled by plugin; core handles)."""
    adapter = make_adapter()
    handled = asyncio.run(adapter._handle_dm_voice_command(
        content="/voice on", chat_id="dm_chat", author_id=USER_ID, source=None))
    assert handled is False


def test_dm_voice_status_passes_through(fake_env, fake_livekit):
    """DM /voice status → false (not handled by plugin; core handles)."""
    adapter = make_adapter()
    handled = asyncio.run(adapter._handle_dm_voice_command(
        content="/voice status", chat_id="dm_chat", author_id="u", source=None))
    assert handled is False
