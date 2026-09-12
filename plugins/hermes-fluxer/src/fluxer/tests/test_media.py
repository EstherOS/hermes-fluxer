"""Unit tests for wave 2 — attachments (spec §6-W2): ``fluxer.media`` + the
adapter's inbound caching and outbound ``send_*`` media methods.

Run::

    cd /home/agent/.hermes/hermes-agent
    ./venv/bin/python -m pytest /home/agent/workspace/fluxer/plugin-src/fluxer/tests -q

No network: aiohttp is faked for the download helper; the REST client is a
recording stub.  Covers: mime → MessageType mapping (voice-note flag), the
inbound size cap, download failure tolerance, strongest-media selection,
outbound upload→claim→create flow, caption chunking, voice-flag attempt +
400-retry, failure mapping (never a fake success), multi-image batching, and
command pass-through (``/new`` reaches the handler unmangled).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import fluxer.adapter as adapter_mod
import fluxer.media as media_mod
from fluxer.adapter import FluxerAdapter, MAX_MESSAGE_LENGTH
from fluxer.media import AttachmentDownloadError, VOICE_MESSAGE_FLAG
from fluxer.rest import FluxerAPIError

from gateway.platforms.event import MessageType

BOT_ID = "1547828742208888832"
OTHER_ID = "1473728643747861346"
CHANNEL = "1547815091221561347"
GUILD = "1547815091221561344"

logger_name = "fluxer.adapter"


# ── helpers ──────────────────────────────────────────────────────────────────

def make_adapter(rest=None, **extra) -> FluxerAdapter:
    adapter = FluxerAdapter(config=SimpleNamespace(extra=dict(extra)))
    adapter._bot_id = BOT_ID
    adapter._write_runtime_status_safe = lambda *a, **k: None  # never touch a real HERMES_HOME
    if rest is not None:
        adapter._rest = rest
    return adapter


def attachment(*, filename="a.png", content_type="image/png", size=3, url="https://media.test/a.png",
               att_id="att1", flags=0) -> dict:
    return {"id": att_id, "filename": filename, "content_type": content_type,
            "size": size, "url": url, "flags": flags}


def payload(*, author_id=OTHER_ID, content="hello", bot=False, channel_id=CHANNEL,
            channel_type=0, guild_id=GUILD, message_id="m1", attachments=None,
            flags=None, username="kairo") -> dict:
    data = {
        "id": message_id, "channel_id": channel_id, "content": content,
        "channel_type": channel_type, "guild_id": guild_id,
        "timestamp": "2026-09-11T08:00:00.000Z",
        "author": {"id": author_id, "username": username, "bot": bot},
    }
    if attachments is not None:
        data["attachments"] = attachments
    if flags is not None:
        data["flags"] = flags
    return data


def run_event(adapter, data):
    """Feed one MESSAGE_CREATE payload through the adapter; return captured events."""
    captured = []

    async def fake_handle(event):
        captured.append(event)

    adapter.handle_message = fake_handle
    adapter._message_handler = fake_handle
    asyncio.run(adapter._handle_message_create(data))
    return captured


class FakeRESTMedia:
    """Recording REST stub with injectable per-call results/errors."""

    def __init__(self):
        self.calls = []
        self.uploads = []          # (channel_id, path, filename)
        self.uploaded = []         # bytes read at upload time (temp files may be gone later)
        self.creates = []          # {channel_id, content, attachments, flags, message_reference}
        self.create_results = []   # exceptions/results popped before default behavior
        self.upload_error = None
        self.create_error = None
        self.create_error_on_flags = False
        self._seq = 0

    async def upload_attachment(self, channel_id, file_path, *, content_type=None, filename=None):
        self.uploads.append((str(channel_id), str(file_path), filename))
        try:
            self.uploaded.append(Path(file_path).read_bytes())
        except OSError:
            self.uploaded.append(b"")
        if self.upload_error is not None:
            raise self.upload_error
        return {"id": 0, "filename": filename or Path(file_path).name,
                "upload_filename": f"up-{Path(file_path).name}",
                "file_size": Path(file_path).stat().st_size,
                "content_type": content_type or "application/octet-stream"}

    async def create_message(self, channel_id, *, content=None, attachments=None,
                             message_reference=None, flags=None, **kwargs):
        self.calls.append(("create_message", channel_id))
        self.creates.append({"channel_id": str(channel_id), "content": content,
                             "attachments": attachments, "flags": flags,
                             "message_reference": message_reference})
        if self.create_results:
            result = self.create_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        if self.create_error_on_flags and flags is not None:
            raise FluxerAPIError(400, "flags not in sendable set", code="INVALID_FORM_BODY")
        if self.create_error is not None:
            raise self.create_error
        self._seq += 1
        return {"id": f"msg-{self._seq}"}

    async def close(self):
        pass


@pytest.fixture
def fake_env(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(adapter_mod, "_get_scoped_secret",
                        lambda name, default=None: values.get(name, default))
    return values


@pytest.fixture
def inbound_env(fake_env):
    fake_env["FLUXER_ALLOW_ALL_USERS"] = "true"
    return fake_env


# ── media.message_type_for ───────────────────────────────────────────────────

def test_message_type_for_image():
    assert media_mod.message_type_for("image/png") is MessageType.PHOTO
    assert media_mod.message_type_for("IMAGE/JPEG; charset=binary") is MessageType.PHOTO


def test_message_type_for_audio_default_is_audio():
    assert media_mod.message_type_for("audio/ogg") is MessageType.AUDIO
    assert media_mod.message_type_for("audio/mpeg; codecs=opus") is MessageType.AUDIO


def test_message_type_for_audio_voice_note_flag():
    assert media_mod.message_type_for("audio/ogg", is_voice_note=True) is MessageType.VOICE


def test_message_type_for_video():
    assert media_mod.message_type_for("video/mp4") is MessageType.VIDEO


def test_message_type_for_document_and_unknown():
    assert media_mod.message_type_for("text/plain") is MessageType.DOCUMENT
    assert media_mod.message_type_for("application/zip") is MessageType.DOCUMENT
    assert media_mod.message_type_for("") is MessageType.DOCUMENT
    assert media_mod.message_type_for(None) is MessageType.DOCUMENT


# ── media.download_attachment (fake aiohttp) ─────────────────────────────────

class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, n):
        for chunk in self._chunks:
            yield chunk


class _FakeResp:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(list(chunks))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return self._resp


def _fake_aiohttp(monkeypatch, resp):
    monkeypatch.setattr(media_mod.aiohttp, "ClientSession", lambda **kw: _FakeSession(resp))


def test_download_attachment_returns_bytes(monkeypatch):
    _fake_aiohttp(monkeypatch, _FakeResp(chunks=[b"abc", b"def"]))
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1024)
    assert asyncio.run(media_mod.download_attachment("https://media.test/x.png")) == b"abcdef"


def test_download_attachment_http_error(monkeypatch):
    _fake_aiohttp(monkeypatch, _FakeResp(status=404))
    with pytest.raises(AttachmentDownloadError) as exc:
        asyncio.run(media_mod.download_attachment("https://media.test/gone.png"))
    assert exc.value.status == 404


def test_download_attachment_size_cap_streamed(monkeypatch):
    _fake_aiohttp(monkeypatch, _FakeResp(chunks=[b"x" * 8, b"x" * 8]))
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 10)
    with pytest.raises(AttachmentDownloadError, match="too large"):
        asyncio.run(media_mod.download_attachment("https://media.test/big.bin"))


def test_download_attachment_size_cap_content_length(monkeypatch):
    _fake_aiohttp(monkeypatch, _FakeResp(headers={"Content-Length": "999"}, chunks=[]))
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 10)
    with pytest.raises(AttachmentDownloadError, match="too large"):
        asyncio.run(media_mod.download_attachment("https://media.test/big.bin"))


def test_download_attachment_rejects_non_http_url():
    with pytest.raises(AttachmentDownloadError, match="invalid attachment url"):
        asyncio.run(media_mod.download_attachment("file:///etc/passwd"))


# ── media.cache_inbound_attachments ──────────────────────────────────────────

def _patch_caches(monkeypatch, calls):
    def make(kind):
        def fake(data, *args):
            calls.append((kind, data, args))
            return f"/cache/{kind}-{len(calls)}{args[0] if args else ''}"
        return fake
    monkeypatch.setattr(media_mod, "cache_image_from_bytes", make("image"))
    monkeypatch.setattr(media_mod, "cache_audio_from_bytes", make("audio"))
    monkeypatch.setattr(media_mod, "cache_video_from_bytes", make("video"))
    monkeypatch.setattr(media_mod, "cache_document_from_bytes", make("document"))


def test_cache_inbound_happy_path_image(monkeypatch):
    calls = []
    _patch_caches(monkeypatch, calls)

    async def fake_download(url, **kwargs):
        return b"png-bytes"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    urls, types, mt = asyncio.run(media_mod.cache_inbound_attachments([attachment(size=9)]))
    assert urls == ["/cache/image-1.png"]
    assert types == ["image/png"]
    assert mt is MessageType.PHOTO
    assert calls[0][0] == "image"


def test_cache_inbound_uses_audio_and_video_helpers(monkeypatch):
    calls = []
    _patch_caches(monkeypatch, calls)

    async def fake_download(url, **kwargs):
        return b"data"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    atts = [attachment(filename="v.mp4", content_type="video/mp4", size=4, att_id="v"),
            attachment(filename="a.ogg", content_type="audio/ogg", size=4, att_id="a")]
    urls, types, mt = asyncio.run(media_mod.cache_inbound_attachments(atts))
    assert [c[0] for c in calls] == ["video", "audio"]
    assert types == ["video/mp4", "audio/ogg"]
    assert mt is MessageType.AUDIO  # audio outranks video (image > audio > video > doc)


def test_cache_inbound_strongest_is_image(monkeypatch):
    calls = []
    _patch_caches(monkeypatch, calls)

    async def fake_download(url, **kwargs):
        return b"data"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    atts = [attachment(filename="v.mp4", content_type="video/mp4", att_id="v"),
            attachment(filename="p.png", content_type="image/png", att_id="p")]
    _urls, _types, mt = asyncio.run(media_mod.cache_inbound_attachments(atts))
    assert mt is MessageType.PHOTO


def test_cache_inbound_voice_note_flag_selects_voice(monkeypatch):
    calls = []
    _patch_caches(monkeypatch, calls)

    async def fake_download(url, **kwargs):
        return b"data"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    atts = [attachment(filename="note.ogg", content_type="audio/ogg", att_id="n")]
    _urls, _types, mt = asyncio.run(
        media_mod.cache_inbound_attachments(atts, flags=VOICE_MESSAGE_FLAG))
    assert mt is MessageType.VOICE
    # per-attachment flag works the same way
    atts = [attachment(filename="note.ogg", content_type="audio/ogg", att_id="n",
                       flags=VOICE_MESSAGE_FLAG)]
    _urls, _types, mt = asyncio.run(media_mod.cache_inbound_attachments(atts))
    assert mt is MessageType.VOICE
    # ...and voice outranks plain audio within one message
    atts = [attachment(filename="song.mp3", content_type="audio/mpeg", att_id="s"),
            attachment(filename="note.ogg", content_type="audio/ogg", att_id="n",
                       flags=VOICE_MESSAGE_FLAG)]
    _urls, _types, mt = asyncio.run(media_mod.cache_inbound_attachments(atts))
    assert mt is MessageType.VOICE


def test_cache_inbound_skips_oversize(monkeypatch):
    calls = []
    _patch_caches(monkeypatch, calls)
    downloaded = []

    async def fake_download(url, **kwargs):
        downloaded.append(url)
        return b"data"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 100)

    urls, types, mt = asyncio.run(
        media_mod.cache_inbound_attachments([attachment(size=1000)]))
    assert (urls, types, mt) == ([], [], None)
    assert downloaded == []  # never pulled the bytes
    assert calls == []


def test_cache_inbound_download_failure_tolerated(monkeypatch, caplog):
    calls = []
    _patch_caches(monkeypatch, calls)
    seq = {"n": 0}

    async def fake_download(url, **kwargs):
        seq["n"] += 1
        if seq["n"] == 1:
            raise AttachmentDownloadError("boom")
        return b"data"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    atts = [attachment(filename="bad.png", att_id="bad"),
            attachment(filename="good.png", att_id="good")]
    with caplog.at_level(logging.WARNING, logger="fluxer.media"):
        urls, types, mt = asyncio.run(media_mod.cache_inbound_attachments(atts))
    assert len(urls) == 1 and types == ["image/png"]
    assert mt is MessageType.PHOTO
    assert "download failed" in caplog.text


def test_cache_inbound_empty_input():
    assert asyncio.run(media_mod.cache_inbound_attachments([])) == ([], [], None)
    assert asyncio.run(media_mod.cache_inbound_attachments(None)) == ([], [], None)


def test_cache_inbound_document_keeps_filename_hint(monkeypatch):
    recorded = {}
    monkeypatch.setattr(media_mod, "cache_image_from_bytes", lambda *a: "/nope")
    monkeypatch.setattr(media_mod, "cache_audio_from_bytes", lambda *a: "/nope")
    monkeypatch.setattr(media_mod, "cache_video_from_bytes", lambda *a: "/nope")

    def fake_doc(data, filename):
        recorded["filename"] = filename
        return "/cache/doc1"

    monkeypatch.setattr(media_mod, "cache_document_from_bytes", fake_doc)

    async def fake_download(url, **kwargs):
        return b"notes"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    urls, types, mt = asyncio.run(media_mod.cache_inbound_attachments(
        [attachment(filename="notes.txt", content_type="text/plain", size=5)]))
    assert urls == ["/cache/doc1"]
    assert types == ["text/plain"]
    assert mt is MessageType.DOCUMENT
    assert recorded["filename"] == "notes.txt"


def test_cache_inbound_mime_falls_back_to_filename(monkeypatch):
    calls = []
    _patch_caches(monkeypatch, calls)

    async def fake_download(url, **kwargs):
        return b"data"

    monkeypatch.setattr(media_mod, "download_attachment", fake_download)
    monkeypatch.setattr(media_mod, "get_inbound_media_max_bytes", lambda: 1000)

    atts = [attachment(filename="photo.jpg", content_type=None, size=4)]
    _urls, types, mt = asyncio.run(media_mod.cache_inbound_attachments(atts))
    assert types == ["image/jpeg"]
    assert mt is MessageType.PHOTO


# ── adapter inbound: attachments → MessageEvent ──────────────────────────────

def test_inbound_attachments_cached_into_event(inbound_env, monkeypatch, caplog):
    captured_args = {}

    async def fake_cache(attachments, *, flags=0):
        captured_args["attachments"] = attachments
        captured_args["flags"] = flags
        return (["/cache/img1.png"], ["image/png"], MessageType.PHOTO)

    monkeypatch.setattr(adapter_mod, "cache_inbound_attachments", fake_cache)
    adapter = make_adapter(free_response_channels=[CHANNEL])
    with caplog.at_level(logging.INFO, logger=logger_name):
        events = run_event(adapter, payload(
            content="check this", attachments=[attachment()]))
    assert len(events) == 1
    event = events[0]
    assert event.text == "check this"
    assert event.media_urls == ["/cache/img1.png"]
    assert event.media_types == ["image/png"]
    assert event.message_type is MessageType.PHOTO
    assert "cached 1 attachment(s)" in caplog.text


def test_inbound_voice_note_flag_reaches_media(inbound_env, monkeypatch):
    captured = {}

    async def fake_cache(attachments, *, flags=0):
        captured["flags"] = flags
        return (["/cache/a.ogg"], ["audio/ogg"], MessageType.VOICE)

    monkeypatch.setattr(adapter_mod, "cache_inbound_attachments", fake_cache)
    adapter = make_adapter(free_response_channels=[CHANNEL])
    events = run_event(adapter, payload(
        content="voice note", attachments=[attachment(filename="a.ogg", content_type="audio/ogg")],
        flags=VOICE_MESSAGE_FLAG))
    assert captured["flags"] == VOICE_MESSAGE_FLAG
    assert events[0].message_type is MessageType.VOICE


def test_inbound_media_failure_still_delivers_text(inbound_env, monkeypatch, caplog):
    async def broken_cache(*args, **kwargs):
        raise RuntimeError("caching exploded")

    monkeypatch.setattr(adapter_mod, "cache_inbound_attachments", broken_cache)
    adapter = make_adapter(free_response_channels=[CHANNEL])
    with caplog.at_level(logging.WARNING, logger=logger_name):
        events = run_event(adapter, payload(
            content="text must survive", attachments=[attachment()]))
    assert len(events) == 1
    assert events[0].text == "text must survive"
    assert events[0].media_urls == []
    assert events[0].message_type is MessageType.TEXT
    assert "attachment caching failed" in caplog.text


def test_inbound_attachment_only_message_delivers_media(inbound_env, monkeypatch):
    async def fake_cache(attachments, *, flags=0):
        return (["/cache/v1.mp4"], ["video/mp4"], MessageType.VIDEO)

    monkeypatch.setattr(adapter_mod, "cache_inbound_attachments", fake_cache)
    adapter = make_adapter(free_response_channels=[CHANNEL])
    events = run_event(adapter, payload(
        content="", attachments=[attachment(filename="v.mp4", content_type="video/mp4")]))
    assert len(events) == 1
    assert events[0].text == ""
    assert events[0].media_urls == ["/cache/v1.mp4"]
    assert events[0].message_type is MessageType.VIDEO


def test_inbound_unretrievable_attachment_gets_placeholder(inbound_env, monkeypatch):
    async def empty_cache(attachments, *, flags=0):
        return ([], [], None)

    monkeypatch.setattr(adapter_mod, "cache_inbound_attachments", empty_cache)
    adapter = make_adapter(free_response_channels=[CHANNEL])
    events = run_event(adapter, payload(content="", attachments=[attachment()]))
    assert len(events) == 1
    assert "could not be retrieved" in events[0].text
    assert events[0].message_type is MessageType.TEXT


# ── adapter outbound: send_image_file / send_image / send_voice / … ─────────

def _local_file(tmp_path, name: str, data: bytes = b"file-bytes") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_send_image_file_success(fake_env, tmp_path):
    rest = FakeRESTMedia()
    adapter = make_adapter(rest)
    path = _local_file(tmp_path, "cat.png")
    result = asyncio.run(adapter.send_image_file(CHANNEL, path, caption="a cat"))
    assert result.success is True
    assert result.message_id == "msg-1"
    assert rest.uploads == [(CHANNEL, path, None)]
    assert rest.creates[0]["attachments"] == [{
        "id": 0, "filename": "cat.png", "upload_filename": "up-cat.png",
        "file_size": len(b"file-bytes"), "content_type": "application/octet-stream"}]
    assert rest.creates[0]["content"] == "a cat"


def test_send_image_file_missing_file(fake_env):
    rest = FakeRESTMedia()
    result = asyncio.run(make_adapter(rest).send_image_file(CHANNEL, "/nope/missing.png"))
    assert result.success is False
    assert "not found" in result.error
    assert rest.uploads == [] and rest.creates == []


def test_send_media_upload_5xx_is_retryable(fake_env, tmp_path):
    rest = FakeRESTMedia()
    rest.upload_error = FluxerAPIError(500, "server exploded")
    result = asyncio.run(make_adapter(rest).send_image_file(CHANNEL, _local_file(tmp_path, "x.png")))
    assert result.success is False and result.retryable is True
    assert rest.creates == []  # failed upload never degrades to a text send


def test_send_media_create_400_not_retryable(fake_env, tmp_path):
    rest = FakeRESTMedia()
    rest.create_error = FluxerAPIError(400, "bad body", code="INVALID_FORM_BODY")
    result = asyncio.run(make_adapter(rest).send_image_file(CHANNEL, _local_file(tmp_path, "x.png")))
    assert result.success is False and result.retryable is False
    assert result.error_kind  # mapped, never a fake success


def test_send_media_caption_chunks_ride_along(fake_env, tmp_path):
    rest = FakeRESTMedia()
    adapter = make_adapter(rest)
    caption = ("word " * 1600).strip()  # ~8k chars → several 4000-char chunks
    result = asyncio.run(adapter.send_image_file(CHANNEL, _local_file(tmp_path, "x.png"), caption=caption))
    assert result.success is True
    assert len(rest.creates) >= 2
    # first chunk rides the media message, which carries the attachment
    assert rest.creates[0]["attachments"] is not None
    assert rest.creates[0]["content"]
    # continuation chunks are plain sends (no attachment, never truncated away)
    for follow in rest.creates[1:]:
        assert follow["attachments"] is None
        assert follow["content"]
        assert len(follow["content"]) <= MAX_MESSAGE_LENGTH
    assert len(result.continuation_message_ids) == len(rest.creates)


def test_send_media_caption_continuation_failure_reports_failure(fake_env, tmp_path):
    rest = FakeRESTMedia()
    adapter = make_adapter(rest)
    caption = ("word " * 1600).strip()
    rest.create_results = [{"id": "msg-media"}, FluxerAPIError(500, "boom")]
    result = asyncio.run(adapter.send_image_file(CHANNEL, _local_file(tmp_path, "x.png"), caption=caption))
    assert result.success is False
    assert result.message_id == "msg-media"       # the media did go out
    assert "continuation" in result.error


def test_send_voice_attempts_voice_flag(fake_env, tmp_path):
    rest = FakeRESTMedia()
    result = asyncio.run(make_adapter(rest).send_voice(CHANNEL, _local_file(tmp_path, "note.wav")))
    assert result.success is True
    assert len(rest.creates) == 1
    assert rest.creates[0]["flags"] == VOICE_MESSAGE_FLAG


def test_send_voice_flag_400_retries_without(fake_env, tmp_path, caplog):
    rest = FakeRESTMedia()
    rest.create_error_on_flags = True
    adapter = make_adapter(rest)
    with caplog.at_level(logging.INFO, logger=logger_name):
        result = asyncio.run(adapter.send_voice(CHANNEL, _local_file(tmp_path, "note.wav")))
    assert result.success is True
    assert len(rest.creates) == 2
    assert rest.creates[0]["flags"] == VOICE_MESSAGE_FLAG
    assert rest.creates[1]["flags"] is None  # retried without the flag
    assert "retrying without the flag" in caplog.text


def test_send_video_success(fake_env, tmp_path):
    rest = FakeRESTMedia()
    result = asyncio.run(make_adapter(rest).send_video(CHANNEL, _local_file(tmp_path, "v.mp4")))
    assert result.success is True
    assert rest.creates[0]["attachments"][0]["filename"] == "v.mp4"
    assert rest.creates[0]["flags"] is None


def test_send_document_file_name_override(fake_env, tmp_path):
    rest = FakeRESTMedia()
    result = asyncio.run(make_adapter(rest).send_document(
        CHANNEL, _local_file(tmp_path, "tmp123.bin"), file_name="report.txt", caption="report"))
    assert result.success is True
    assert rest.uploads == [(CHANNEL, str(tmp_path / "tmp123.bin"), "report.txt")]
    assert rest.creates[0]["content"] == "report"


def test_send_media_not_connected(fake_env):
    adapter = make_adapter()
    for call in (
        adapter.send_image_file(CHANNEL, "/x.png"),
        adapter.send_image(CHANNEL, "https://example.test/x.png"),
        adapter.send_voice(CHANNEL, "/x.wav"),
        adapter.send_video(CHANNEL, "/x.mp4"),
        adapter.send_document(CHANNEL, "/x.txt"),
    ):
        result = asyncio.run(call)
        assert result.success is False and result.retryable is True


def test_send_image_url_downloads_then_uploads(fake_env, monkeypatch, tmp_path):
    rest = FakeRESTMedia()
    seen = {}

    async def fake_download(url, **kwargs):
        seen["url"] = url
        return b"GIF89a-fake-image"

    monkeypatch.setattr(adapter_mod, "download_attachment", fake_download)
    result = asyncio.run(make_adapter(rest).send_image(
        CHANNEL, "https://fluxerusercontent.com/attachments/1/2/pic.png", caption="pic"))
    assert result.success is True
    assert seen["url"] == "https://fluxerusercontent.com/attachments/1/2/pic.png"
    uploaded_path = rest.uploads[0][1]
    assert uploaded_path.endswith(".png")
    assert rest.uploads[0][2] == "pic.png"      # upload filename from the URL
    assert rest.uploaded[-1] == b"GIF89a-fake-image"
    assert not Path(uploaded_path).exists()     # temp file cleaned up


def test_send_image_url_download_failure_is_honest(fake_env, monkeypatch):
    rest = FakeRESTMedia()

    async def fake_download(url, **kwargs):
        raise AttachmentDownloadError("HTTP 403")

    monkeypatch.setattr(adapter_mod, "download_attachment", fake_download)
    result = asyncio.run(make_adapter(rest).send_image(CHANNEL, "https://x.test/y.png"))
    assert result.success is False
    assert "download failed" in result.error
    assert rest.creates == []


def test_send_image_local_path_and_file_url(fake_env, tmp_path):
    rest = FakeRESTMedia()
    adapter = make_adapter(rest)
    path = _local_file(tmp_path, "local.jpg")
    assert asyncio.run(adapter.send_image(CHANNEL, path)).success is True
    assert asyncio.run(adapter.send_image(CHANNEL, f"file://{path}")).success is True
    assert [u[1] for u in rest.uploads] == [path, path]


def test_send_multiple_images_single_message(fake_env, tmp_path):
    rest = FakeRESTMedia()
    files = [(_local_file(tmp_path, f"i{i}.png", bytes([i])), f"alt{i}") for i in range(3)]
    images = [(f"file://{path}", alt) for path, alt in files]
    result = asyncio.run(make_adapter(rest).send_multiple_images(CHANNEL, images))
    assert result.success is True
    assert len(rest.creates) == 1
    assert len(rest.creates[0]["attachments"]) == 3
    assert rest.creates[0]["content"] == "alt0"


def test_send_multiple_images_chunks_over_ten(fake_env, tmp_path):
    rest = FakeRESTMedia()
    images = [(f"file://{_local_file(tmp_path, f'i{i}.png', bytes([i]))}", "") for i in range(11)]
    result = asyncio.run(make_adapter(rest).send_multiple_images(CHANNEL, images))
    assert result.success is True
    assert len(rest.creates) == 2
    assert len(rest.creates[0]["attachments"]) == 10
    assert len(rest.creates[1]["attachments"]) == 1


def test_send_multiple_images_all_missing(fake_env):
    rest = FakeRESTMedia()
    result = asyncio.run(make_adapter(rest).send_multiple_images(
        CHANNEL, [("/nope/a.png", ""), ("/nope/b.png", "")]))
    assert result.success is False
    assert rest.creates == []


# ── command pass-through (never mangled by mention strip / attachment logic) ─

def test_command_new_reaches_handler_unchanged(inbound_env):
    adapter = make_adapter(free_response_channels=[CHANNEL])
    events = run_event(adapter, payload(content="/new"))
    assert len(events) == 1
    assert events[0].text == "/new"


def test_command_after_mention_reaches_handler(inbound_env):
    adapter = make_adapter(free_response_channels=[CHANNEL])
    events = run_event(adapter, payload(content=f"<@{BOT_ID}> /stop"))
    assert len(events) == 1
    assert events[0].text == "/stop"


def test_command_with_attachment_keeps_text_and_media(inbound_env, monkeypatch):
    async def fake_cache(attachments, *, flags=0):
        return (["/cache/img.png"], ["image/png"], MessageType.PHOTO)

    monkeypatch.setattr(adapter_mod, "cache_inbound_attachments", fake_cache)
    adapter = make_adapter(free_response_channels=[CHANNEL])
    events = run_event(adapter, payload(content="/new", attachments=[attachment()]))
    assert len(events) == 1
    assert events[0].text == "/new"
    assert events[0].media_urls == ["/cache/img.png"]


def test_slash_text_inside_message_not_treated_as_prefix(inbound_env):
    adapter = make_adapter(require_mention=False)
    events = run_event(adapter, payload(content="please run /new tomorrow"))
    assert len(events) == 1
    assert events[0].text == "please run /new tomorrow"
