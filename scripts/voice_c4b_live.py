#!/usr/bin/env python3
"""Wave-3 bounded live evidence for the Fluxer voice module (C4b).

Phases (bounded, nice'd; whole run capped by ``--total-timeout``):

  a  controller join → LiveKit connected → module-speak (piper) → clean leave
     (reuses the c4a probe mechanics; tokens never logged — redacted descriptor).
  b  STT ears offline: piper sentence WAV → STT backend → transcript must match
     words.  whisper.cpp (offline, vendored tiny.en) runs first; the Hermes
     faster-whisper path runs in a bounded subprocess (fresh HERMES_HOME).
  c  cascade dry-run: the piper WAV fed through the cascade internals as if
     received — VAD → STT → mock handle_message → reply → TTS → PCM published
     through a real livekit AudioSource (fake transport); intermediates captured.
  d  missing-deps simulation: subprocess hides ``livekit`` entirely and proves
     the plugin still imports and stays text-only with one clear log line.

Evidence: redacted JSONL + summary under ``status/c4b-evidence/``.

    nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/voice_c4b_live.py --phase all
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKOUT = pathlib.Path("/home/agent/.hermes/hermes-agent")
sys.path.insert(0, str(ROOT / "plugin-src"))
sys.path.insert(0, str(CHECKOUT))
# The dry-run dispatches through the real adapter's allowlist gate (fail-closed):
os.environ.setdefault("FLUXER_ALLOW_ALL_USERS", "true")

from voice_probe import Evidence, load_token, token_detail  # noqa: E402

from gateway.config import Platform  # noqa: E402

# Same dynamic-platform registration the gateway's plugin registry performs.
if "fluxer" not in Platform._value2member_map_:
    Platform._add_pseudo_member("fluxer")

from fluxer.adapter import FluxerAdapter  # noqa: E402
from fluxer.gatewayws import FluxerGatewayClient  # noqa: E402
from fluxer.rest import FluxerREST  # noqa: E402
from fluxer.voice import audio as audio_lib  # noqa: E402
from fluxer.voice.config import parse_voice_config  # noqa: E402
from fluxer.voice.controller import VoiceController  # noqa: E402

GUILD = "1547815091221561344"
VOICE_CHANNEL = "1547815091221561348"
BOT_ID = "1547828742208888832"
PIPER = ROOT / "gpu" / "tools" / "piper" / "piper"
PIPER_MODEL = ROOT / "models" / "en_US-lessac-medium.onnx"
SPEECH_WAV = pathlib.Path("/tmp/c4b-voice.wav")
SPEECH_TEXT = "The wave three voice module is online and hearing every word."

VOICE_CFG = {
    "enabled": True,
    "session_binding": "channel",
    "transcripts": "off",
    "stt": {"engine": "whispercpp", "threads": 2},
    "tts": {"engine": "piper", "binary": str(PIPER), "model": str(PIPER_MODEL)},
    "silence_ms": 900,
    "min_utterance_ms": 300,
    "energy_threshold": 400.0,
    "rejoin_max_attempts": 2,
    "join_timeout_s": 20.0,
}


# ── helpers ──────────────────────────────────────────────────────────────────

def make_adapter(ev: Evidence) -> FluxerAdapter:
    adapter = FluxerAdapter(config=SimpleNamespace(extra={"voice": dict(VOICE_CFG)}))
    adapter._bot_id = BOT_ID
    adapter._voice_cfg = parse_voice_config(dict(VOICE_CFG), fallback_guild=None)
    # avoid touching any real HERMES_HOME from the standalone script
    adapter._write_runtime_status_safe = lambda *a, **k: None
    return adapter


def ensure_piper_wav(ev: Evidence) -> pathlib.Path:
    if SPEECH_WAV.exists() and SPEECH_WAV.stat().st_size > 0:
        ev.rec("piper_wav", status="reused", path=str(SPEECH_WAV),
               bytes=SPEECH_WAV.stat().st_size)
        return SPEECH_WAV
    proc = subprocess.run(
        [str(PIPER), "--model", str(PIPER_MODEL), "--output_file", str(SPEECH_WAV)],
        input=SPEECH_TEXT.encode(), capture_output=True, timeout=90, check=True)
    ev.rec("piper_wav", status="generated", path=str(SPEECH_WAV),
           bytes=SPEECH_WAV.stat().st_size,
           tail=(proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-2:])
    return SPEECH_WAV


class DryRunRoom:
    """Minimal room stand-in for the cascade dry-run (no wire, real AudioSource)."""

    class _Participant:
        identity = f"user_{BOT_ID}_dryrun"

        def __init__(self):
            self.published = []

        async def publish_track(self, track, options=None):
            self.published.append((track, getattr(options, "source", None)))
            return SimpleNamespace(sid="TR_dryrun", name=getattr(track, "name", "hermes-voice"),
                                   source=2, muted=False)

    def __init__(self):
        self.local_participant = self._Participant()
        self.remote_participants: dict = {}
        self._handlers: dict = {}

    def on(self, name, cb):
        self._handlers.setdefault(name, []).append(cb)


# ── phase a: live join → speak → leave ───────────────────────────────────────

async def phase_a(ev: Evidence, summary: dict, hold: float = 1.5) -> None:
    from livekit import rtc
    import importlib.metadata as md

    ev.rec("phase_a_start", livekit=md.version("livekit"), python=sys.version.split()[0],
           nice=os.nice(0), guild=GUILD, channel=VOICE_CHANNEL)

    token = load_token()
    adapter = make_adapter(ev)
    gateway = FluxerGatewayClient(token, on_event=lambda t, d: adapter._on_gateway_event(t, d),
                                  on_connection_event=None, start_timeout=30.0)
    rest = FluxerREST(token)
    adapter._rest = rest
    try:
        t0 = time.monotonic()
        await gateway.start()
        adapter._ws = gateway
        ev.rec("gateway_ready", seconds=round(time.monotonic() - t0, 3),
               user_id=gateway.user_id, session_id=gateway.session_id)
        summary["phases"]["gateway_ready"] = "ok"

        ctl = adapter._voice_controller()
        if ctl is None:
            raise RuntimeError("voice controller unavailable (livekit import failed?)")

        t0 = time.monotonic()
        ok = await ctl.join(GUILD, VOICE_CHANNEL)
        session = ctl.session_for_channel(VOICE_CHANNEL)
        ev.rec("join", ok=ok, seconds=round(time.monotonic() - t0, 3),
               binding_chat_id=(session.binding_chat_id if session else None),
               binding_chat_type=(session.binding_chat_type if session else None),
               connection_id=(session.connection_id if session else None))
        if not ok or session is None:
            summary["phases"]["join"] = "failed"
            return
        summary["phases"]["join"] = "ok"

        # record room events (additive to the controller's own handlers)
        room_events: list[str] = []
        for name in ("connected", "disconnected", "reconnecting", "reconnected",
                     "local_track_published", "track_published", "track_subscribed",
                     "connection_state_changed", "token_refreshed",
                     "connection_quality_changed", "active_speakers_changed"):
            def handler(event_name):
                def cb(*args):
                    room_events.append(event_name)
                    ev.rec("room_event", event=event_name,
                           args=[str(a)[:160] for a in args[:2]])
                return cb
            session.room.on(name, handler(name))

        room_sid = session.room.sid
        if not isinstance(room_sid, str):   # livekit 1.1.x: Room.sid is async
            room_sid = await room_sid
        ev.rec("room", name=session.room.name, sid=str(room_sid),
               local_identity=session.room.local_participant.identity,
               remote=len(session.room.remote_participants))
        summary["phases"]["livekit_connect"] = "ok"

        spoken = await ctl.speak("Hello from the wave three voice module. "
                                 "The cascade ear and mouth are connected.",
                                 session=session)
        ev.rec("speak", ok=spoken, session_stats=dict(session.stats),
               publication_sid=session.publication_sid)
        summary["phases"]["speak"] = "ok" if spoken else "failed"
        if session.track is not None:
            stats = await session.track.get_stats()
            texts = [str(s).replace("\n", " ") for s in stats]
            ev.rec("track_stats", count=len(stats),
                   wire=[t[:900] for t in texts
                         if "packets_sent" in t or "candidate_pair" in t.lower()
                         or "PAIR_" in t or "total_audio_energy" in t])
        if session.source is not None:
            try:
                await asyncio.wait_for(session.source.wait_for_playout(), 5.0)
                ev.rec("playout_drained", status="ok")
            except Exception as exc:
                ev.rec("playout_drained", status=f"{type(exc).__name__}: {exc}")

        ev.rec("phase_a_room_events", counts={n: room_events.count(n) for n in sorted(set(room_events))})

        await ctl.leave(GUILD, reason="evidence-done")
        ev.rec("leave", session_after=ctl.session_for_channel(VOICE_CHANNEL) is None,
               controller_stats=dict(ctl.stats))
        summary["phases"]["leave"] = "ok"
        summary["controller_stats"] = dict(ctl.stats)
    finally:
        try:
            await gateway.stop()
        except Exception as exc:
            ev.rec("gateway_stop_error", error=str(exc))
        try:
            await rest.close()
        except Exception:
            pass


# ── phase b: STT ears offline ────────────────────────────────────────────────

def _words(text: str) -> set[str]:
    return {w.strip(".,!?").lower() for w in (text or "").split() if len(w) > 2}


def phase_b(ev: Evidence, summary: dict, *, hermes_timeout: float = 120.0) -> None:
    from fluxer.voice.cascade import _hermes_transcribe, _whispercpp_transcribe

    wav = ensure_piper_wav(ev)
    expected = _words(SPEECH_TEXT)
    ev.rec("phase_b_start", wav=str(wav), expected_sample=sorted(expected)[:6])

    # 1) whisper.cpp (offline, vendored tiny.en, 2 threads)
    cfg_cpp = parse_voice_config(dict(VOICE_CFG))
    t0 = time.monotonic()
    try:
        text_cpp = _whispercpp_transcribe(cfg_cpp, str(wav))
    except Exception as exc:  # pragma: no cover - evidence only
        text_cpp = None
        ev.rec("stt_whispercpp_error", error=f"{type(exc).__name__}: {exc}")
    overlap = len(expected & _words(text_cpp or "")) / max(len(expected), 1)
    ev.rec("stt_whispercpp", seconds=round(time.monotonic() - t0, 3), transcript=text_cpp,
           word_overlap=round(overlap, 3))
    summary["phases"]["stt_whispercpp"] = "ok" if overlap >= 0.5 else "weak"

    # 2) Hermes faster-whisper path — bounded subprocess with a scratch HERMES_HOME
    scratch = pathlib.Path("/tmp/c4b-hermes-home")
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "config.yaml").write_text(
        "stt:\n  enabled: true\n  language: en\n  provider: local\n"
        "  local:\n    model: tiny\n    device: cpu\n    compute_type: int8\n",
        encoding="utf-8")
    child = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(CHECKOUT)!r}); sys.path.insert(0, {str(ROOT / 'plugin-src')!r})\n"
        "from fluxer.voice.cascade import _hermes_transcribe\n"
        "from fluxer.voice.config import parse_voice_config\n"
        f"cfg = parse_voice_config({{'stt': {{'engine': 'hermes', 'model': 'tiny'}}}})\n"
        f"print(json.dumps({{'transcript': _hermes_transcribe(cfg, {str(wav)!r})}}))\n"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(scratch)
    env["HF_HOME"] = str(scratch / "hf")
    env["OMP_NUM_THREADS"] = "2"
    t0 = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True,
                              text=True, timeout=hermes_timeout, env=env)
        parsed = None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
        text_h = (parsed or {}).get("transcript")
        overlap_h = len(expected & _words(text_h or "")) / max(len(expected), 1)
        ev.rec("stt_hermes", seconds=round(time.monotonic() - t0, 3), transcript=text_h,
               word_overlap=round(overlap_h, 3), returncode=proc.returncode,
               stderr_tail=(proc.stderr or "").strip().splitlines()[-4:])
        summary["phases"]["stt_hermes"] = "ok" if overlap_h >= 0.5 else "weak"
    except subprocess.TimeoutExpired:
        ev.rec("stt_hermes", seconds=round(time.monotonic() - t0, 3), status="timeout",
               note="model download/first load exceeded the bound; whispercpp path stands in")
        summary["phases"]["stt_hermes"] = "timeout"
    summary["stt_expected_overlap"] = {"whispercpp": round(overlap, 3)}


# ── phase c: cascade dry-run ─────────────────────────────────────────────────

async def phase_c(ev: Evidence, summary: dict) -> None:
    from livekit import rtc
    from fluxer.voice.cascade import VoiceCascade

    wav = ensure_piper_wav(ev)
    samples, info = audio_lib.load_wav_48k_mono(wav)
    ev.rec("phase_c_start", wav=str(wav), seconds=info.out_seconds)

    adapter = make_adapter(ev)
    adapter._rest = None
    cfg = parse_voice_config(dict(VOICE_CFG))
    ctl = VoiceController(adapter, cfg)          # real controller (real piper for the reply)

    received: list = []

    async def mock_handle_message(event):
        received.append(event)

    adapter.handle_message = mock_handle_message
    adapter._message_handler = mock_handle_message

    session = ctl._make_session(GUILD, VOICE_CHANNEL, source=None, text_channel_id=None)
    session.rtc = rtc
    session.room = DryRunRoom()
    session.connection_id = "dry-run"
    ctl._register_session(session)
    session.cascade = VoiceCascade(session, adapter, cfg)
    session.cascade.start()
    ev.rec("dryrun_session", binding_chat_id=session.binding_chat_id,
           binding_chat_type=session.binding_chat_type, state="connected")

    # feed the piper WAV + a trailing 1.2 s of silence (as if the speaker stopped)
    receive_buffer = samples.tolist() + audio_lib.silence(1.2).tolist()
    receive = audio_lib.array.array("h", receive_buffer)
    frames = 0
    for i in range(0, len(receive), audio_lib.FRAME_SAMPLES):
        chunk = receive[i:i + audio_lib.FRAME_SAMPLES]
        await session.cascade.process_frame_bytes(bytes(chunk.tobytes()),
                                                  participant=SimpleNamespace(
                                                      identity="user_1473728643747861346_dryrun"))
        frames += 1
    ev.rec("dryrun_frames_fed", frames=frames, seconds=round(len(receive) / audio_lib.SAMPLE_RATE, 3))

    for _ in range(200):
        await asyncio.sleep(0.02)
        if received:
            break
    cascade_stats = session.cascade.snapshot()
    ev.rec("dryrun_cascade", **cascade_stats)
    if not received:
        summary["phases"]["cascade"] = "no_dispatch"
        return
    event = received[0]
    ev.rec("dryrun_dispatch", text=event.text, chat_id=event.source.chat_id,
           chat_type=event.source.chat_type, user_id=event.source.user_id,
           message_type=str(event.message_type))
    summary["phases"]["cascade"] = "ok"
    summary["cascade_transcript"] = event.text

    # reply → TTS → PCM publish (real AudioSource/frames; fake transport).
    # A recording AudioSource subclass taps the EXACT PCM handed to livekit.
    captured = {"frames": 0, "bytes": 0}

    class RecordingAudioSource(rtc.AudioSource):
        async def capture_frame(self, frame):
            data = bytes(frame.data.cast("B"))
            captured["frames"] += 1
            captured["bytes"] += len(data)
            await super().capture_frame(frame)

    shim = SimpleNamespace(**{k: getattr(rtc, k) for k in dir(rtc) if not k.startswith("_")})
    shim.AudioSource = RecordingAudioSource
    session.rtc = shim
    reply = "Dry run reply. The lights are on and the kettle is boiling."
    spoken = await ctl.speak(reply, session=session, tag="dryrun-reply")
    frames_published = captured["frames"] or session.stats["frames_published"]
    frame_bytes = captured["bytes"]
    ev.rec("dryrun_reply_spoken", ok=spoken, session_stats=dict(session.stats),
           publish_calls=len(getattr(session.room.local_participant, "published", [])),
           publication_sid=session.publication_sid,
           pcm_captured={"frames": captured["frames"], "bytes": frame_bytes})
    summary["phases"]["cascade_reply"] = "ok" if spoken else "failed"
    summary["dryrun_pcm"] = {"frames": frames_published, "bytes": frame_bytes,
                             "expected_seconds": round(frame_bytes / 2 / audio_lib.SAMPLE_RATE, 2)}
    await session.cascade.stop()


# ── phase d: missing-deps simulation ─────────────────────────────────────────

def phase_d(ev: Evidence, summary: dict) -> None:
    child = r"""
import logging, sys, types
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
sys.modules["livekit"] = None  # `from livekit import rtc` now raises ImportError
sys.path.insert(0, {checkout!r})
sys.path.insert(0, {plugin!r})
from types import SimpleNamespace
from gateway.config import Platform
if "fluxer" not in Platform._value2member_map_:
    Platform._add_pseudo_member("fluxer")
import fluxer.adapter as a
adapter = a.FluxerAdapter(config=SimpleNamespace(extra={{}}))
adapter._bot_id = "1"
import asyncio
ok = asyncio.run(adapter.join_voice_channel(SimpleNamespace(id="c", guild_id="g")))
print("IMPORT_OK", adapter._voice_controller() is None, ok)
""".format(checkout=str(CHECKOUT), plugin=str(ROOT / "plugin-src"))
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                          timeout=60, env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    ev.rec("phase_d_start", returncode=proc.returncode,
           import_ok="IMPORT_OK" in (proc.stdout or ""),
           hint_logged="install livekit into the Hermes venv" in out,
           output_tail=out.strip().splitlines()[-4:])
    summary["phases"]["missing_deps"] = (
        "ok" if ("IMPORT_OK" in (proc.stdout or "")
                 and "install livekit into the Hermes venv" in out) else "check_output")


# ── main ─────────────────────────────────────────────────────────────────────

async def amain(args, ev: Evidence, summary: dict) -> None:
    if args.phase in ("a", "all"):
        await phase_a(ev, summary, hold=args.hold)
    if args.phase in ("b", "all"):
        await asyncio.to_thread(phase_b, ev, summary, hermes_timeout=args.hermes_timeout)
    if args.phase in ("c", "all"):
        await phase_c(ev, summary)
    if args.phase in ("d", "all"):
        await asyncio.to_thread(phase_d, ev, summary)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=["a", "b", "c", "d", "all"], default="all")
    ap.add_argument("--hold", type=float, default=1.5)
    ap.add_argument("--hermes-timeout", type=float, default=120.0)
    ap.add_argument("--total-timeout", type=float, default=230.0)
    ap.add_argument("--evidence-dir", type=pathlib.Path, default=ROOT / "status" / "c4b-evidence")
    args = ap.parse_args()

    try:
        os.nice(10)
    except Exception:
        pass

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    ev = Evidence(args.evidence_dir / f"voice-c4b-{stamp}.jsonl")
    summary: dict = {"status": "unknown", "phases": {}}
    try:
        summary = asyncio.run(asyncio.wait_for(amain(args, ev, summary), args.total_timeout)) or summary
        summary["status"] = "completed"
    except asyncio.TimeoutError:
        summary["status"] = "global_timeout"
        ev.rec("global_timeout", timeout_s=args.total_timeout)
    except Exception as exc:
        summary["status"] = "error"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        ev.rec("script_error", error=summary["error"])
    finally:
        summary["evidence_jsonl"] = str(ev.path)
        out = ev.path.with_suffix(".summary.json")
        out.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"[summary] {json.dumps(summary, default=str)[:900]}", flush=True)
        print(f"[evidence] {ev.path} ({len(ev.records)} records)", flush=True)
        ev.close()


if __name__ == "__main__":
    main()
