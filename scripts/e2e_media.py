#!/usr/bin/env python3
"""Live E2E for the fluxer plugin's wave-2 attachment support (C3 evidence).

Standalone adapter (no sandbox gateway — the scoped bot lock must be free;
disconnect at the end), bounded and nice'd:

    nice -n 10 /home/agent/.hermes/hermes-agent/venv/bin/python \
        /home/agent/workspace/fluxer/scripts/e2e_media.py

Phases (all against #general ``1547815091221561347``):

1. OUTBOUND — ``send_image_file`` (models/test-dog.jpg), ``send_voice``
   (models/piper-test.wav, VOICE_MESSAGE flag attempted), ``send_video``
   (ffmpeg-synthesized ~1s 128x128 mp4) and ``send_document`` (small txt),
   each with a ``[c3-e2e]`` caption.  Adapter logs are captured at INFO.
2. READ-BACK — REST ``list_messages``: every message must carry an attachment
   with the right filename; voice/video/document sizes must equal the source
   byte-for-byte (image sizes may differ: Fluxer normalizes uploaded images —
   recorded, not a failure).  The document URL is downloaded with
   ``media.download_attachment`` and byte-compared to the source; the image URL
   is probed for declared-size match + JPEG magic.
3. INBOUND — read-back message JSON through ``media.cache_inbound_attachments``
   (cached file must exist and match the download/source bytes) and through
   ``adapter._handle_message_create`` with the author rewritten to a non-self
   user and a mock ``_message_handler`` (the event must carry
   ``media_urls``/``media_types``/``message_type``) — run for the document
   (byte-exact) and the image (PHOTO class).
4. CLEANUP — every created message is deleted; the channel is re-listed and the
   run exits non-zero if any tracked id or ``[c3-e2e]`` marker survives.

Bounded probe mode::

    ... scripts/e2e_media.py --voice-note-only

sends ONE captionless voice note (documents the platform's VOICE_MESSAGE
requirements — a caption 400s with ``VOICE_MESSAGES_CANNOT_HAVE_CONTENT``;
without ``waveform`` data the flag 400s with
``VOICE_MESSAGES_ATTACHMENT_WAVEFORM_REQUIRED`` — and the adapter's
retry-without-flag fallback), reads it back (flags/attachment), byte-compares the
download, deletes it and writes ``status/c3-evidence/e2e_media_voicenote.json``.

Token + ids come from ``/home/agent/workspace/fluxer/.env`` (never printed).
Raw evidence is written to ``status/c3-evidence/e2e_media_run.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = Path("/home/agent/.hermes/hermes-agent")
ENV_FILE = ROOT / ".env"
PLUGIN_SRC = ROOT / "plugin-src"
sys.path.insert(0, str(PLUGIN_SRC))
sys.path.insert(0, str(CHECKOUT))

CHANNEL = "1547815091221561347"          # #general in the test guild
GUILD = "1547815091221561344"
TAG = "[c3-e2e]"
STEP_SLEEP = 1.2                         # between live writes (rate-limit hygiene)
STEP_TIMEOUT = 90.0                      # per live step
EVIDENCE_DIR = ROOT / "status" / "c3-evidence"
STATE_PATH = ROOT / "sandbox" / "c3-e2e-state.json"

IMAGE = ROOT / "models" / "test-dog.jpg"
VOICE = ROOT / "models" / "piper-test.wav"
VIDEO = Path("/tmp/c3_e2e_video.mp4")
DOCUMENT = Path("/tmp/c3_e2e_doc.txt")


# ── small helpers ────────────────────────────────────────────────────────────

def load_env() -> None:
    """Load FLUXER_* keys from the workspace .env into os.environ (never echoed)."""
    if not ENV_FILE.is_file():
        sys.exit(f"env file not found: {ENV_FILE}")
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key.startswith("FLUXER_") and key not in os.environ:
            os.environ[key] = value


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def synth_video(path: Path) -> None:
    """~1s 128x128 test mp4 (idempotent; bounded)."""
    if path.exists() and path.stat().st_size > 0:
        return
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=128x128:rate=10", "-pix_fmt", "yuv420p",
         "-y", str(path)],
        check=True, timeout=60,
    )


class LogCapture(logging.Handler):
    """Keeps adapter INFO logs for the evidence file (e.g. the voice-flag retry line)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover - never break logging
            pass


class Evidence:
    """Collects structured step records; written to status/c3-evidence/."""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.start = time.monotonic()

    def add(self, step: str, ok: bool, **data) -> None:
        entry = {"step": step, "ok": bool(ok), "t_s": round(time.monotonic() - self.start, 2)}
        entry.update(data)
        self.steps.append(entry)
        print(f"[c3-e2e] {step}: {'PASS' if ok else 'FAIL'} "
              f"{json.dumps(data, default=str)[:500]}", flush=True)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "when": datetime.now(timezone.utc).isoformat(),
            "channel": CHANNEL,
            "tag": TAG,
            "steps": self.steps,
            "pass": all(s["ok"] for s in self.steps),
        }, indent=2) + "\n")


def save_state(ids: list[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"message_ids": ids}, indent=2) + "\n")


def load_state_ids() -> list[str]:
    if not STATE_PATH.exists():
        return []
    try:
        return [str(i) for i in json.loads(STATE_PATH.read_text()).get("message_ids", [])]
    except (ValueError, OSError):
        return []


# ── main flow ────────────────────────────────────────────────────────────────

async def main() -> int:
    load_env()
    # The mocked inbound leg feeds a synthetic (non-self) author; allow it locally
    # for this process only — never written anywhere, never a live gateway env.
    os.environ.setdefault("FLUXER_ALLOW_ALL_USERS", "true")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log_capture = LogCapture()
    logging.getLogger().addHandler(log_capture)

    from gateway.config import Platform
    if "fluxer" not in Platform._value2member_map_:
        Platform._add_pseudo_member("fluxer")
    from fluxer.adapter import FluxerAdapter
    from fluxer.media import cache_inbound_attachments, download_attachment

    ev = Evidence()
    created_ids: list[str] = []
    adapter = None
    rest = None
    all_ok = True

    # assets
    synth_video(VIDEO)
    DOCUMENT.write_text("fluxer plugin / c3 e2e attachment test\n")
    non_self_id = str(os.environ.get("FLUXER_USER_ID") or "9990000000000000003")

    adapter = FluxerAdapter(config=SimpleNamespace(extra={"free_response_channels": [CHANNEL]}))
    # never touch a real HERMES_HOME from a standalone script
    adapter._write_runtime_status_safe = lambda *a, **k: None

    try:
        print(f"[c3-e2e] connecting standalone adapter (token from {ENV_FILE.name}, not printed)",
              flush=True)
        ok = await asyncio.wait_for(adapter.connect(), timeout=STEP_TIMEOUT)
        ev.add("connect", bool(ok), bot_id=adapter._bot_id)
        all_ok = all_ok and bool(ok)
        if not ok:
            return 2
        rest = adapter._rest
        assert rest is not None, "adapter connected without a REST client"

        # pre-run channel snapshot (read-only) for the cleanup comparison
        pre_messages = await asyncio.wait_for(rest.list_messages(CHANNEL, limit=50),
                                              timeout=STEP_TIMEOUT)
        pre_ids = {str(m.get("id")) for m in pre_messages or []}
        ev.add("pre_snapshot", True, message_count=len(pre_ids))

        # ── phase 1: outbound media ──────────────────────────────────────────
        sends = [
            ("image", lambda: adapter.send_image_file(
                CHANNEL, str(IMAGE), caption=f"{TAG} image test")),
            ("voice", lambda: adapter.send_voice(
                CHANNEL, str(VOICE), caption=f"{TAG} voice test")),
            ("video", lambda: adapter.send_video(
                CHANNEL, str(VIDEO), caption=f"{TAG} video test")),
            ("document", lambda: adapter.send_document(
                CHANNEL, str(DOCUMENT), caption=f"{TAG} document test")),
        ]
        sent: dict[str, dict] = {}
        for kind, call in sends:
            result = await asyncio.wait_for(call(), timeout=STEP_TIMEOUT)
            entry = {"message_id": result.message_id, "success": result.success,
                     "error": result.error, "retryable": result.retryable}
            sent[kind] = entry
            ok = bool(result.success and result.message_id)
            ev.add(f"send_{kind}", ok, **entry)
            all_ok = all_ok and ok
            if result.message_id:
                created_ids.append(str(result.message_id))
                save_state(created_ids)
            await asyncio.sleep(STEP_SLEEP)

        # ── phase 2: read-back + attachment download ─────────────────────────
        expected = {
            "image": (IMAGE, "test-dog.jpg"),
            "voice": (VOICE, "piper-test.wav"),
            "video": (VIDEO, "c3_e2e_video.mp4"),
            "document": (DOCUMENT, "c3_e2e_doc.txt"),
        }
        read_back: dict[str, dict] = {}
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            messages = await asyncio.wait_for(rest.list_messages(CHANNEL, limit=20),
                                              timeout=STEP_TIMEOUT)
            by_id = {str(m.get("id")): m for m in messages or []}
            read_back = {kind: by_id.get(str(entry["message_id"]))
                         for kind, entry in sent.items() if entry.get("message_id")}
            if all(read_back.values()):
                break
            await asyncio.sleep(1.5)

        for kind, (source, fname) in expected.items():
            msg = read_back.get(kind) or {}
            atts = msg.get("attachments") or []
            att = atts[0] if atts else {}
            src_bytes = source.read_bytes()
            size = int(att.get("size") or 0)
            # voice/video/document are byte-exact on Fluxer; images are normalized
            # server-side (re-encoded), so only "same class, sane size" applies.
            if kind == "image":
                ok = bool(att and str(att.get("filename")) == fname and size > 0)
                extra = {"size_delta": size - len(src_bytes), "reencoded": size != len(src_bytes)}
            else:
                ok = bool(att and str(att.get("filename")) == fname and size == len(src_bytes))
                extra = {}
            ev.add(f"readback_{kind}", ok, message_id=msg.get("id"),
                   filename=att.get("filename"), size=att.get("size"),
                   source_size=len(src_bytes), content_type=att.get("content_type"),
                   message_flags=msg.get("flags"), attachment_flags=att.get("flags"),
                   url_host=(str(att.get("url") or "").split("/")[2] if att.get("url") else None),
                   **extra)
            all_ok = all_ok and ok

        downloads: dict[str, bytes] = {}

        # byte-exact download proof on a stable type (document)
        doc_att = ((read_back.get("document") or {}).get("attachments") or [{}])[0]
        doc_url = str(doc_att.get("url") or "")
        if doc_url:
            data = await asyncio.wait_for(download_attachment(doc_url), timeout=STEP_TIMEOUT)
            downloads["document"] = data
            ok = data == DOCUMENT.read_bytes()
            ev.add("download_document_bytes_exact", ok, got=len(data),
                   want=DOCUMENT.stat().st_size, sha256=sha256(data)[:16])
            all_ok = all_ok and ok
        else:
            ev.add("download_document_bytes_exact", False, error="no document url in read-back")
            all_ok = False

        # image download probe: declared size matches fetched bytes; JPEG magic intact
        image_att = ((read_back.get("image") or {}).get("attachments") or [{}])[0]
        image_url = str(image_att.get("url") or "")
        if image_url:
            data = await asyncio.wait_for(download_attachment(image_url), timeout=STEP_TIMEOUT)
            downloads["image"] = data
            ok = (len(data) == int(image_att.get("size") or 0)
                  and data[:3] == b"\xff\xd8\xff")
            ev.add("download_image_probe", ok, got=len(data),
                   declared=int(image_att.get("size") or 0), jpeg_magic=data[:3].hex(),
                   sha256=sha256(data)[:16])
            all_ok = all_ok and ok
        else:
            ev.add("download_image_probe", False, error="no image url in read-back")
            all_ok = False

        # ── phase 3: inbound — cache_inbound_attachments ─────────────────────
        for kind in ("image", "voice", "video", "document"):
            msg = read_back.get(kind) or {}
            source, _fname = expected[kind]
            if not msg.get("attachments"):
                ev.add(f"cache_{kind}", False, error="no attachments on read-back message")
                all_ok = False
                continue
            urls, types, mtype = await asyncio.wait_for(
                cache_inbound_attachments(msg["attachments"], flags=int(msg.get("flags") or 0)),
                timeout=STEP_TIMEOUT)
            path = Path(urls[0]) if urls else None
            if kind == "image":
                cached = path.read_bytes() if path and path.is_file() else b""
                reference = downloads.get("image", b"")
                ok = bool(path and path.is_file() and cached == reference and cached[:3] == b"\xff\xd8\xff"
                          and mtype is not None)
            else:
                ok = bool(path and path.is_file() and path.read_bytes() == source.read_bytes()
                          and mtype is not None)
            ev.add(f"cache_{kind}", ok, media_url=str(path), media_type=types[0] if types else None,
                   message_type=getattr(mtype, "value", None),
                   file_exists=bool(path and path.is_file()),
                   sha256=(sha256(path.read_bytes())[:16] if path and path.is_file() else None))
            all_ok = all_ok and ok

        # ── phase 3b: full adapter inbound path with a mocked handler ────────
        async def feed_message(kind: str) -> tuple[dict, list]:
            feed = json.loads(json.dumps(read_back.get(kind) or {}))
            feed["author"] = {"id": non_self_id, "username": "c3-e2e-external",
                              "global_name": None, "bot": False}
            feed["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            captured: list = []

            async def _capture(event):
                captured.append(event)

            adapter._message_handler = _capture
            adapter.handle_message = _capture
            await asyncio.wait_for(adapter._handle_message_create(feed), timeout=STEP_TIMEOUT)
            return feed, captured

        for kind in ("document", "image"):
            source, _fname = expected[kind]
            _feed, captured = await feed_message(kind)
            if captured:
                event = captured[0]
                paths_ok = bool(event.media_urls) and all(Path(p).is_file() for p in event.media_urls)
                if kind == "image":
                    bytes_ok = bool(event.media_urls) and \
                        Path(event.media_urls[0]).read_bytes() == downloads.get("image", b"\x00")
                    expect_type = "photo"
                    expect_mime = "image/jpeg"
                else:
                    bytes_ok = bool(event.media_urls) and \
                        Path(event.media_urls[0]).read_bytes() == source.read_bytes()
                    expect_type = "document"
                    expect_mime = "text/plain"
                ok = bool(paths_ok and bytes_ok
                          and event.media_types == [expect_mime]
                          and getattr(event.message_type, "value", None) == expect_type
                          and event.text == f"{TAG} {kind} test")
                ev.add(f"adapter_inbound_event_{kind}", ok,
                       text=event.text, media_urls=list(event.media_urls),
                       media_types=list(event.media_types),
                       message_type=getattr(event.message_type, "value", None),
                       source_user=event.source.user_id, source_chat=event.source.chat_id)
                all_ok = all_ok and ok
            else:
                ev.add(f"adapter_inbound_event_{kind}", False, error="no handle_message call")
                all_ok = False

        # voice-flag outcome (informational; the retry path is logged at INFO)
        retried = any("retrying without the flag" in line for line in log_capture.lines)
        ev.add("voice_flag_outcome", True,
               retried_without_flag=retried,
               voice_message_flags=(read_back.get("voice") or {}).get("flags"),
               voice_attachment_flags=(
                   ((read_back.get("voice") or {}).get("attachments") or [{}])[0].get("flags")))

    except Exception as e:  # noqa: BLE001 - report, never crash the cleanup path
        ev.add("exception", False, error=f"{type(e).__name__}: {e}")
        all_ok = False
    finally:
        # ── phase 4: cleanup — delete what we created, verify the channel ────
        if rest is not None:
            leftover: list[str] = []
            tracked = list(dict.fromkeys(created_ids + load_state_ids()))
            for message_id in tracked:
                try:
                    await asyncio.wait_for(rest.delete_message(CHANNEL, message_id), timeout=30)
                    print(f"[c3-e2e] deleted message {message_id}", flush=True)
                except Exception as e:  # noqa: BLE001 - 404 = already gone
                    if getattr(e, "status", None) == 404:
                        print(f"[c3-e2e] message {message_id} already gone", flush=True)
                    else:
                        print(f"[c3-e2e] COULD NOT delete {message_id}: {e}", flush=True)
                        leftover.append(message_id)
            await asyncio.sleep(1.0)
            # verify by read-back: tracked ids gone + no [c3-e2e] marker by the bot
            try:
                messages = await asyncio.wait_for(rest.list_messages(CHANNEL, limit=50),
                                                  timeout=30)
                current_ids = {str(m.get("id")) for m in messages or []}
                stray = [str(m.get("id")) for m in messages or []
                         if str((m.get("author") or {}).get("id")) == str(adapter._bot_id)
                         and TAG in str(m.get("content") or "")]
                remaining_tracked = sorted(set(tracked) & current_ids)
                ok = not remaining_tracked and not stray
                pre_ids = locals().get("pre_ids") or set()
                ev.add("cleanup_verified", ok, deleted=len(tracked),
                       remaining_tracked=remaining_tracked, stray=stray,
                       channel_message_count=len(current_ids),
                       pre_snapshot_count=len(pre_ids),
                       new_non_test_messages=sorted(current_ids - pre_ids - set(tracked)))
                all_ok = all_ok and ok
            except Exception as e:  # noqa: BLE001
                ev.add("cleanup_verified", False, error=f"{type(e).__name__}: {e}")
                all_ok = False
            save_state([])
        if adapter is not None and adapter._rest is not None:
            try:
                await asyncio.wait_for(adapter.disconnect(), timeout=30)
                print("[c3-e2e] adapter disconnected", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[c3-e2e] disconnect error: {e}", flush=True)
        ev.write(EVIDENCE_DIR / "e2e_media_run.json")

    print(f"\n[c3-e2e] RESULT: {'PASS' if all_ok else 'FAIL'} "
          f"({sum(1 for s in ev.steps if s['ok'])}/{len(ev.steps)} checks)", flush=True)
    return 0 if all_ok else 2


async def voice_note_probe() -> int:
    """Bounded probe: ONE captionless voice note through ``send_voice`` (native
    VOICE_MESSAGE 8192 path — Fluxer rejects the flag when text content rides
    along), read back + byte-compared + deleted.  Writes its own evidence file."""
    load_env()
    os.environ.setdefault("FLUXER_ALLOW_ALL_USERS", "true")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log_capture = LogCapture()
    logging.getLogger().addHandler(log_capture)

    from gateway.config import Platform
    if "fluxer" not in Platform._value2member_map_:
        Platform._add_pseudo_member("fluxer")
    from fluxer.adapter import FluxerAdapter
    from fluxer.media import download_attachment

    ev = Evidence()
    created: list[str] = []
    all_ok = True
    adapter = FluxerAdapter(config=SimpleNamespace(extra={}))
    adapter._write_runtime_status_safe = lambda *a, **k: None
    try:
        ok = await asyncio.wait_for(adapter.connect(), timeout=STEP_TIMEOUT)
        ev.add("connect", bool(ok), bot_id=adapter._bot_id)
        if not ok:
            return 2
        rest = adapter._rest

        result = await asyncio.wait_for(adapter.send_voice(CHANNEL, str(VOICE)),
                                        timeout=STEP_TIMEOUT)
        ok = bool(result.success and result.message_id)
        ev.add("send_voice_note", ok, message_id=result.message_id, error=result.error)
        all_ok = all_ok and ok
        if result.message_id:
            created.append(str(result.message_id))
        await asyncio.sleep(STEP_SLEEP)

        msg = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and msg is None:
            messages = await asyncio.wait_for(rest.list_messages(CHANNEL, limit=10),
                                              timeout=STEP_TIMEOUT)
            msg = next((m for m in messages or []
                        if str(m.get("id")) == str(result.message_id)), None)
            if msg is None:
                await asyncio.sleep(1.0)
        att = ((msg or {}).get("attachments") or [{}])[0]
        retried = any("retrying without the flag" in line for line in log_capture.lines)
        ok = bool(msg and att.get("filename")) and int(att.get("size") or 0) == VOICE.stat().st_size
        ev.add("voice_note_readback", ok, filename=att.get("filename"), size=att.get("size"),
               message_flags=(msg or {}).get("flags"), attachment_flags=att.get("flags"),
               message_type=(msg or {}).get("type"), retried_without_flag=retried)
        all_ok = all_ok and ok

        if att.get("url"):
            data = await asyncio.wait_for(download_attachment(str(att["url"])),
                                          timeout=STEP_TIMEOUT)
            exact = data == VOICE.read_bytes()
            ev.add("voice_note_bytes_exact", exact, got=len(data),
                   want=VOICE.stat().st_size, sha256=sha256(data)[:16])
            all_ok = all_ok and exact
    except Exception as e:  # noqa: BLE001
        ev.add("exception", False, error=f"{type(e).__name__}: {e}")
        all_ok = False
    finally:
        rest = adapter._rest
        if rest is not None:
            for message_id in created:
                try:
                    await asyncio.wait_for(rest.delete_message(CHANNEL, message_id), timeout=30)
                    print(f"[c3-e2e] deleted voice-note message {message_id}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[c3-e2e] COULD NOT delete {message_id}: {e}", flush=True)
                    all_ok = False
            try:
                messages = await asyncio.wait_for(rest.list_messages(CHANNEL, limit=50),
                                                  timeout=30)
                remaining = {str(m.get("id")) for m in messages or []} & set(created)
                ev.add("cleanup_verified", not remaining, remaining_tracked=sorted(remaining),
                       channel_message_count=len(messages or []))
                all_ok = all_ok and not remaining
            except Exception as e:  # noqa: BLE001
                ev.add("cleanup_verified", False, error=f"{type(e).__name__}: {e}")
                all_ok = False
        try:
            await asyncio.wait_for(adapter.disconnect(), timeout=30)
        except Exception:  # noqa: BLE001
            pass
        ev.write(EVIDENCE_DIR / "e2e_media_voicenote.json")
    print(f"\n[c3-e2e] VOICE-NOTE PROBE: {'PASS' if all_ok else 'FAIL'} "
          f"({sum(1 for s in ev.steps if s['ok'])}/{len(ev.steps)} checks)", flush=True)
    return 0 if all_ok else 2


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="fluxer wave-2 attachment live E2E")
    parser.add_argument("--voice-note-only", action="store_true",
                        help="bounded probe: one captionless native voice note (VOICE_MESSAGE 8192)")
    args = parser.parse_args()
    if args.voice_note_only:
        sys.exit(asyncio.run(voice_note_probe()))
    sys.exit(asyncio.run(main()))
