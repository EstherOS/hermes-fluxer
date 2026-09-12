#!/usr/bin/env python3
"""Wave-2 voice feasibility probe (C4a) — Fluxer op4 join → LiveKit room → piper publish.

Run from the repo root (Hermes venv, nice'd, bounded):

    nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python scripts/voice_probe.py

What it does, in order (all steps bounded; whole run capped at --total-timeout):

1. REST sanity check: GET /guilds/{guild}/channels → confirm the voice channel
   exists, is type 2, and read its limits.
2. Gateway (C1 ``FluxerGatewayClient``, reused via sys.path): connect → READY →
   read voice-relevant state (session, rtc_regions count, GUILD_CREATE
   ``voice_states``).
3. op 4 Voice State Update → join ``--channel`` (self_mute=False, self_deaf=False).
   Wait for the session-only ``VOICE_SERVER_UPDATE`` (token/endpoint/connection_id).
   The LiveKit grant token is a credential: it is NEVER written to evidence —
   only ``len``/``prefix`` + the non-secret JWT *claim subset* (grants) are kept.
4. LiveKit connect: ``rtc.Room().connect(endpoint, token)`` (``wss://`` prepended
   when the server sends a host-only endpoint). Log room name/SID/state + participants.
5. Publish piper TTS: 48 kHz mono Int16 frames from the piper WAV (generated on
   demand) through a published microphone ``AudioSource``; then ~--hold seconds of
   silence; collect track stats + events (published? subscribers? errors).
6. Leave: op 4 with ``channel_id=null``, room disconnect, gateway stop.

Evidence: a redacted JSONL trace + a summary JSON under ``status/c4a-evidence/``
(``--evidence-dir``).  Console output is meant to be teed to a ``.log`` by the caller.

Token handling: the bot token is read from ``sandbox/hermes-home/.env`` (or
``FLUXER_ENV_FILE`` / ``$FLUXER_BOT_TOKEN``) and never printed.  Any ``token`` /
``e2ee_key`` field in recorded payloads is replaced by ``{redacted, len, prefix}``.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import wave

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKOUT = pathlib.Path("/home/agent/.hermes/hermes-agent")
sys.path.insert(0, str(ROOT / "plugin-src"))
sys.path.insert(0, str(CHECKOUT))  # parity with tests/conftest.py + e2e_text.py

from fluxer.gatewayws import FluxerGatewayClient  # noqa: E402
from fluxer.rest import FluxerREST  # noqa: E402

GUILD = "1547815091221561344"
VOICE_CHANNEL = "1547815091221561348"   # type 2 "General" under "Voice Channels"
BOT_ID = "1547828742208888832"

PIPER = ROOT / "gpu" / "tools" / "piper" / "piper"
PIPER_MODEL = ROOT / "models" / "en_US-lessac-medium.onnx"
DEFAULT_WAV = pathlib.Path("/tmp/vp.wav")
PIPER_TEXT = "Hello from the night shift. The Fluxer voice path is alive."

REDACT_KEYS = {"token", "e2ee_key", "access_token", "authorization"}
AUDIO_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms @ 48 kHz


# ── helpers ──────────────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def redact_deep(obj):
    """Replace credential-looking values with {redacted, len, prefix}."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in REDACT_KEYS and isinstance(v, str):
                out[k] = {"redacted": True, "len": len(v), "prefix": v[:8]}
            else:
                out[k] = redact_deep(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_deep(x) for x in obj]
    return obj


def token_detail(token: str) -> dict:
    """Non-secret descriptor of a credential: length + short prefix only."""
    return {"redacted": True, "len": len(token), "prefix": token[:8]}


def jwt_claim_subset(token: str) -> dict | None:
    """Decode the (non-secret) JWT payload segment; keep grant-relevant claims only.

    The signature segment is never touched; the raw token is never returned.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None
    keep = ("iss", "sub", "identity", "exp", "iat", "nbf", "video", "name", "metadata")
    out = {k: claims[k] for k in keep if k in claims}
    if isinstance(out.get("video"), dict):
        v = out["video"]
        out["video"] = {k: v[k] for k in sorted(v) if not k.startswith("_")}
    if "exp" in out and "iat" in out:
        try:
            out["ttl_s"] = int(out["exp"]) - int(out["iat"])
        except Exception:
            pass
    return out


def conn_state_name(value) -> str:
    from livekit import rtc

    return enum_name(rtc.ConnectionState, value)


def enum_name(enum_cls, value) -> str:
    """Best-effort readable name for a proto-enum or python-enum value."""
    try:
        return enum_cls.Name(value)  # protobuf EnumTypeWrapper
    except Exception:
        pass
    try:
        return enum_cls(value).name  # python enum
    except Exception:
        return str(value)


class Evidence:
    """Append-only redacted JSONL trace + in-memory copy for the summary."""

    def __init__(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")
        self.records: list[dict] = []

    def rec(self, kind: str, **fields) -> dict:
        record = {"ts": utc_now(), "kind": kind}
        record.update(redact_deep(fields))
        line = json.dumps(record, default=str)
        self._fh.write(line + "\n")
        self._fh.flush()
        self.records.append(record)
        brief = line if len(line) <= 240 else line[:240] + "…"
        print(f"[ev] {brief}", flush=True)
        return record

    def close(self) -> None:
        self._fh.close()


def load_token() -> str:
    env_file = os.environ.get("FLUXER_ENV_FILE")
    candidates = [pathlib.Path(env_file)] if env_file else [
        ROOT / "sandbox" / "hermes-home" / ".env", ROOT / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("FLUXER_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    token = os.environ.get("FLUXER_BOT_TOKEN")
    if token:
        return token
    sys.exit(f"FLUXER_BOT_TOKEN not found (checked {', '.join(str(c) for c in candidates)})")


def ensure_wav(path: pathlib.Path, ev: Evidence) -> None:
    if path.exists() and path.stat().st_size > 0:
        ev.rec("piper_wav", status="reused", path=str(path), bytes=path.stat().st_size)
        return
    proc = subprocess.run(
        [str(PIPER), "--model", str(PIPER_MODEL), "--output_file", str(path)],
        input=PIPER_TEXT.encode(), capture_output=True, timeout=60, check=True,
    )
    tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-2:]
    ev.rec("piper_wav", status="generated", path=str(path), bytes=path.stat().st_size,
           piper_log_tail=tail)


def load_pcm48k(path: pathlib.Path) -> tuple[array.array, dict]:
    """Read a mono/stereo Int16 WAV and return 48 kHz mono Int16 samples."""
    with wave.open(str(path)) as w:
        channels, rate, width, frames = (
            w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes())
        raw = w.readframes(frames)
    info = {"src_rate": rate, "src_channels": channels, "src_width": width,
            "src_seconds": round(frames / rate, 3)}
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got width={width}")
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1:  # downmix to mono (take channel 0)
        samples = array.array("h", samples[0::channels])
    if rate != AUDIO_RATE:
        n_out = int(len(samples) * AUDIO_RATE / rate)
        out = array.array("h", bytes(2 * n_out))
        last = len(samples) - 1
        for i in range(n_out):
            pos = i * last / max(n_out - 1, 1)
            i0 = int(pos)
            frac = pos - i0
            i1 = min(i0 + 1, last)
            out[i] = int(samples[i0] * (1.0 - frac) + samples[i1] * frac)
        samples = out
    info["out_rate"] = AUDIO_RATE
    info["out_seconds"] = round(len(samples) / AUDIO_RATE, 3)
    return samples, info


def put_samples(frame, samples: array.array) -> None:
    """Bulk-copy Int16 samples into an AudioFrame's buffer."""
    byte_view = frame.data.cast("B")
    if len(samples) * 2 != len(byte_view):
        raise ValueError(f"frame size mismatch: {len(samples)} samples vs {len(byte_view)} bytes")
    byte_view[:] = samples.tobytes()


def compact_stat(stat) -> dict:
    present = [f.name for f in stat.DESCRIPTOR.fields if stat.HasField(f.name)]
    out = {"present": present, "text": str(stat).replace("\n", " ")[:400]}
    try:
        out["type"] = stat.type
    except Exception:
        pass
    return out


# ── probe ────────────────────────────────────────────────────────────────────


async def run(args, ev: Evidence) -> dict:
    summary: dict = {"phases": {}, "counts": {}}
    import importlib.metadata as md
    from livekit import rtc

    ev.rec("probe_start", guild=args.guild, channel=args.channel,
           livekit=md.version("livekit"), python=sys.version.split()[0],
           nice=os.nice(0), wav=str(args.wav), hold_s=args.hold)

    # 1. REST sanity check -----------------------------------------------------
    rest = FluxerREST(load_token())
    try:
        channels = await rest.request("GET", f"/guilds/{args.guild}/channels")
        voice = [c for c in channels if str(c.get("id")) == str(args.channel)]
        others = [(c["id"], c.get("type"), c.get("name")) for c in channels]
        ev.rec("rest_channel_check", found=bool(voice), channel=voice[0] if voice else None,
               all_channels=others)
        summary["phases"]["rest_channel_check"] = "ok" if voice else "channel_not_found"
        if not voice or int(voice[0].get("type", -1)) != 2:
            raise SystemExit("voice channel missing or not type 2 — stop (needs kairo)")
    except SystemExit:
        raise
    except Exception as exc:
        ev.rec("rest_channel_check", error=f"{type(exc).__name__}: {exc}")
        summary["phases"]["rest_channel_check"] = "error"
    finally:
        await rest.close()

    # 2. gateway ---------------------------------------------------------------
    join_evt = asyncio.Event()
    guild_evt = asyncio.Event()
    vsu: dict = {}
    stats = {"events": {}, "voice_states_self": [], "voice_states_other": 0}

    def count_event(event: str) -> None:
        stats["events"][event] = stats["events"].get(event, 0) + 1

    async def on_event(event: str, d) -> None:
        count_event(event)
        if event == "READY" and isinstance(d, dict):
            user = d.get("user") or {}
            ev.rec("gw_ready", session_id=d.get("session_id"), user_id=user.get("id"),
                   version=d.get("version"), rtc_regions=len(d.get("rtc_regions") or []))
        elif event == "GUILD_CREATE" and isinstance(d, dict) and str(d.get("id")) == args.guild:
            ev.rec("gw_guild_create", guild_id=d.get("id"), voice_states=d.get("voice_states"),
                   channels=len(d.get("channels") or []), members=len(d.get("members") or []))
            guild_evt.set()
        elif event == "VOICE_SERVER_UPDATE":
            vsu.update(d)
            ev.rec("voice_server_update", channel_id=d.get("channel_id"),
                   guild_id=d.get("guild_id"), connection_id=d.get("connection_id"),
                   endpoint=d.get("endpoint"), token=token_detail(str(d.get("token", ""))))
            claims = jwt_claim_subset(str(d.get("token", "")))
            ev.rec("livekit_token_claims", claims=claims)
            join_evt.set()
        elif event == "VOICE_STATE_UPDATE":
            if str((d or {}).get("user_id")) == BOT_ID:
                stats["voice_states_self"].append(d)
                ev.rec("voice_state_self", **(d or {}))
            else:
                stats["voice_states_other"] += 1
        elif event == "VOICE_STATE_ACK":
            ev.rec("voice_state_ack", **(d or {}))
        elif event == "RESUMED":
            ev.rec("gw_resumed")

    async def on_connection(kind: str, payload) -> None:
        ev.rec("gw_connection", connection=kind,
               payload=(payload if kind != "ready" else
                        {"session_id": (payload or {}).get("session_id")}))

    client = FluxerGatewayClient(load_token(), on_event=on_event,
                                 on_connection_event=on_connection, start_timeout=30.0)
    room = rtc.Room()
    room_events: list[str] = []
    participant_types = tuple(
        t for t in (getattr(rtc, n, None)
                    for n in ("Participant", "LocalParticipant", "RemoteParticipant"))
        if isinstance(t, type))

    def room_handler(name):
        def handler(*a):
            room_events.append(name)
            brief = []
            for x in a:
                if participant_types and isinstance(x, participant_types):
                    brief.append({"participant": getattr(x, "identity", None)})
                elif isinstance(x, rtc.LocalTrackPublication):
                    brief.append({"publication": {"sid": x.sid, "name": x.name,
                                                  "source": int(x.source)}})
                elif isinstance(x, bool):
                    brief.append(x)
                elif isinstance(x, int):
                    if name == "connection_quality_changed":
                        brief.append(enum_name(rtc.ConnectionQuality, x))
                    elif name == "disconnected":
                        brief.append(enum_name(rtc.DisconnectReason, x))
                    elif name == "connection_state_changed":
                        brief.append(conn_state_name(x))
                    else:
                        brief.append(x)
                else:
                    brief.append(repr(x)[:120])
            ev.rec("room_event", event=name, args=brief)
        return handler

    for name in ("connected", "disconnected", "reconnecting", "reconnected",
                 "connection_state_changed", "participant_connected",
                 "participant_disconnected", "track_published", "track_unpublished",
                 "track_subscribed", "track_subscription_failed", "local_track_published",
                 "local_track_unpublished", "active_speakers_changed",
                 "connection_quality_changed", "participant_permissions_changed",
                 "room_updated", "token_refreshed"):
        try:
            room.on(name, room_handler(name))
        except Exception as exc:  # pragma: no cover
            ev.rec("room_handler_registration_failed", event=name, error=str(exc))

    try:
        t0 = time.monotonic()
        await client.start()
        ev.rec("gw_ready_gate", user_id=client.user_id, session_id=client.session_id,
               seconds=round(time.monotonic() - t0, 3))
        summary["phases"]["gateway_ready"] = "ok"
    except Exception as exc:
        summary["phases"]["gateway_ready"] = f"error:{type(exc).__name__}"
        ev.rec("gw_start_error", error=f"{type(exc).__name__}: {exc}")
        await client.stop()
        return summary

    try:
        await asyncio.wait_for(guild_evt.wait(), 5.0)
        summary["phases"]["guild_create_wait"] = "ok"
    except asyncio.TimeoutError:
        summary["phases"]["guild_create_wait"] = "timeout"
        ev.rec("guild_create_timeout", waited_s=5)

    try:
        # 3. op4 join ----------------------------------------------------------
        t0 = time.monotonic()
        await client.update_voice_state(args.guild, args.channel,
                                        self_mute=False, self_deaf=False)
        ev.rec("op4_join_sent", guild_id=args.guild, channel_id=args.channel,
               self_mute=False, self_deaf=False)
        try:
            await asyncio.wait_for(join_evt.wait(), 20.0)
            summary["phases"]["op4_join"] = "ok"
            ev.rec("op4_join_update", seconds_to_server_update=round(time.monotonic() - t0, 3))
        except asyncio.TimeoutError:
            summary["phases"]["op4_join"] = "timeout_no_voice_server_update"
            ev.rec("op4_join_timeout", waited_s=20)
            return summary

        endpoint = str(vsu.get("endpoint") or "")
        url = endpoint if endpoint.startswith(("ws://", "wss://")) else f"wss://{endpoint}"
        ev.rec("livekit_endpoint", raw_endpoint=endpoint, connect_url_scheme=url.split("://")[0])

        # 4. LiveKit connect ---------------------------------------------------
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(room.connect(url, str(vsu.get("token"))), 25.0)
            try:
                room_sid = await room.sid  # async property in livekit 1.1.x
            except Exception as exc:
                room_sid = f"unavailable:{type(exc).__name__}"
            ev.rec("lk_connected", seconds=round(time.monotonic() - t0, 3), name=room.name,
                   sid=room_sid, state=conn_state_name(room.connection_state),
                   local_identity=room.local_participant.identity,
                   remote_participants=list(room.remote_participants.keys()))
            summary["phases"]["livekit_connect"] = "ok"
        except Exception as exc:
            summary["phases"]["livekit_connect"] = f"error:{type(exc).__name__}"
            ev.rec("lk_connect_error", error=f"{type(exc).__name__}: {exc}")
            return summary

        # 5. publish piper audio ----------------------------------------------
        if args.no_publish:
            ev.rec("publish_skipped", reason="--no-publish")
            summary["phases"]["publish"] = "skipped"
        else:
            samples, info = load_pcm48k(args.wav)
            ev.rec("pcm_ready", **info)
            source = rtc.AudioSource(AUDIO_RATE, 1)
            track = rtc.LocalAudioTrack.create_audio_track("hermes-voice-probe", source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            try:
                t0 = time.monotonic()
                pub = await asyncio.wait_for(
                    room.local_participant.publish_track(track, options), 15.0)
                ev.rec("track_published", seconds=round(time.monotonic() - t0, 3),
                       sid=pub.sid, name=pub.name, source=int(pub.source), muted=pub.muted,
                       local_publications=len(room.local_participant.track_publications))
                summary["phases"]["publish"] = "ok"
            except Exception as exc:
                summary["phases"]["publish"] = f"error:{type(exc).__name__}"
                ev.rec("publish_error", error=f"{type(exc).__name__}: {exc}")
                raise

            frames = 0
            t0 = time.monotonic()
            for i in range(0, len(samples), FRAME_SAMPLES):
                chunk = samples[i:i + FRAME_SAMPLES]
                if len(chunk) < FRAME_SAMPLES:
                    chunk = array.array("h", list(chunk) + [0] * (FRAME_SAMPLES - len(chunk)))
                frame = rtc.AudioFrame.create(AUDIO_RATE, 1, FRAME_SAMPLES)
                put_samples(frame, chunk)
                await source.capture_frame(frame)
                frames += 1
            ev.rec("audio_pushed", frames=frames, seconds=info["out_seconds"],
                   wall_s=round(time.monotonic() - t0, 3))

            silence = array.array("h", [0] * FRAME_SAMPLES)
            hold_frames = int(args.hold * AUDIO_RATE / FRAME_SAMPLES)
            t0 = time.monotonic()
            for _ in range(hold_frames):
                frame = rtc.AudioFrame.create(AUDIO_RATE, 1, FRAME_SAMPLES)
                put_samples(frame, silence)
                await source.capture_frame(frame)
            ev.rec("hold_done", hold_s=args.hold, wall_s=round(time.monotonic() - t0, 3))

            try:
                await asyncio.wait_for(source.wait_for_playout(), 8.0)
                ev.rec("playout_drained", status="ok")
            except Exception as exc:
                ev.rec("playout_drained", status=f"error:{type(exc).__name__}: {exc}")

            try:
                stats_raw = await track.get_stats()
                ev.rec("track_stats", count=len(stats_raw),
                       stats=[compact_stat(s) for s in stats_raw])
            except Exception as exc:
                ev.rec("track_stats", error=f"{type(exc).__name__}: {exc}")

            try:
                await room.local_participant.unpublish_track(pub.sid)
                ev.rec("track_unpublished", sid=pub.sid)
            except Exception as exc:
                ev.rec("track_unpublish_error", error=f"{type(exc).__name__}: {exc}")

        # 6. leave -------------------------------------------------------------
        try:
            await client.update_voice_state(args.guild, None)
            ev.rec("op4_leave_sent", guild_id=args.guild, channel_id=None)
        except Exception as exc:
            ev.rec("op4_leave_error", error=f"{type(exc).__name__}: {exc}")
        try:
            await asyncio.wait_for(room.disconnect(), 8.0)
            ev.rec("lk_disconnected_called")
        except Exception as exc:
            ev.rec("lk_disconnect_error", error=f"{type(exc).__name__}: {exc}")

        summary["counts"]["gateway_events"] = stats["events"]
        summary["counts"]["room_events"] = {n: room_events.count(n) for n in sorted(set(room_events))}
        summary["counts"]["voice_states_self"] = len(stats["voice_states_self"])
        return summary
    finally:
        try:
            await asyncio.wait_for(room.disconnect(), 5.0)
            ev.rec("lk_disconnect_cleanup", status="ok")
        except Exception as exc:
            ev.rec("lk_disconnect_cleanup", status=f"{type(exc).__name__}: {exc}")
        try:
            await client.stop()
            ev.rec("gw_stopped")
        except Exception as exc:
            ev.rec("gw_stop_error", error=f"{type(exc).__name__}: {exc}")


async def amain(args, ev: Evidence) -> dict:
    return await asyncio.wait_for(run(args, ev), args.total_timeout)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--guild", default=GUILD)
    ap.add_argument("--channel", default=os.environ.get("FLUXER_VOICE_CHANNEL_ID", VOICE_CHANNEL))
    ap.add_argument("--wav", type=pathlib.Path, default=DEFAULT_WAV,
                    help="piper WAV to publish (generated if missing)")
    ap.add_argument("--hold", type=float, default=5.0, help="seconds of silence after the speech")
    ap.add_argument("--no-publish", action="store_true", help="connect only, skip audio publish")
    ap.add_argument("--total-timeout", type=float, default=170.0)
    ap.add_argument("--evidence-dir", type=pathlib.Path, default=ROOT / "status" / "c4a-evidence")
    args = ap.parse_args()

    try:
        os.nice(10)
    except Exception:
        pass

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    ev = Evidence(args.evidence_dir / f"voice-probe-{stamp}.jsonl")
    summary: dict = {"status": "unknown"}
    try:
        ensure_wav(args.wav, ev)
        summary = asyncio.run(amain(args, ev))
        summary["status"] = "completed"
    except asyncio.TimeoutError:
        summary = {"status": "global_timeout", "timeout_s": args.total_timeout}
        ev.rec("global_timeout", timeout_s=args.total_timeout)
        summary["phases"] = {"global": "timeout"}
    except SystemExit as exc:
        summary = {"status": "stop", "reason": str(exc)}
    except Exception as exc:
        summary = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        ev.rec("probe_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        summary["evidence_jsonl"] = str(ev.path)
        out = ev.path.with_suffix(".summary.json")
        out.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"[summary] {json.dumps(summary, default=str)[:600]}", flush=True)
        print(f"[evidence] {ev.path}  ({len(ev.records)} records)", flush=True)
        ev.close()


if __name__ == "__main__":
    main()
