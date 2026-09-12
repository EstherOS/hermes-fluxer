"""Unit tests for the video duplexer lane (wave 4).

Subprocess work is mocked (``fluxer.video.local_backend.run_command`` /
``fluxer.video.render.run_ffmpeg`` / ``stream_to_ffmpeg``); the live evidence
run under ``status/c5-evidence/`` exercises the real binaries.  Async paths run
via ``asyncio.run`` (no pytest-asyncio in this venv).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import types as pytypes
from pathlib import Path

import pytest

from fluxer.video import local_backend as lb
from fluxer.video import render
from fluxer.video.duplex import Frame, VideoError, VideoEvent

RUN = asyncio.run
PLUGIN_SRC = Path(__file__).resolve().parents[2]


# ── data model ───────────────────────────────────────────────────────────────


def test_frame_validation_and_helpers():
    frame = Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4, format="rgb24", ts=2.5)
    assert frame.channels == 3 and frame.nbytes == 48
    assert frame.meta == {}
    with pytest.raises(VideoError, match="needs 48"):
        Frame(data=b"\x00" * 47, width=4, height=4)
    with pytest.raises(VideoError, match="positive dimensions"):
        Frame(data=b"", width=0, height=4)
    compressed = Frame(data=b"\xff\xd8\xff", width=10, height=10, format="jpeg")
    assert compressed.channels is None


def test_video_event_dict():
    event = VideoEvent(1.0, "Caption", "hello", meta=None)
    assert event.kind == "caption" and event.meta == {}
    assert event.as_dict() == {"ts": 1.0, "kind": "caption", "text": "hello", "meta": {}}


def test_motion_energy_sampled_difference():
    black = Frame(data=b"\x00" * (8 * 8 * 3), width=8, height=8)
    white = Frame(data=b"\xff" * (8 * 8 * 3), width=8, height=8)
    assert lb.motion_energy(black, black) == 0.0
    assert lb.motion_energy(black, white) == 1.0
    assert lb.motion_energy(black, Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4)) is None


# ── argv builders / parsers ─────────────────────────────────────────────────


def test_frame_sampling_argv_matches_verified_shape():
    argv = lb.frame_sampling_argv("ffmpeg", Path("/v/clip.mp4"), Path("/out/dir"), fps=2, width=512)
    assert argv == [
        "ffmpeg", "-v", "error", "-y", "-i", "/v/clip.mp4",
        "-vf", "fps=2,scale=512:-2", "-q:v", "3", "/out/dir/frame_%05d.jpg",
    ]


def test_native_and_frame_caption_argv():
    native = lb.native_video_argv(Path("/llama"), Path("/m.gguf"), Path("/mm.gguf"), Path("/c.mp4"), fps=2)
    assert native[native.index("--video") + 1] == "/c.mp4"
    assert native[native.index("--video-fps") + 1] == "2"
    assert native[native.index("-c") + 1] == "8192"
    assert native[native.index("-p") + 1] == lb.CAPTION_PROMPT

    one = lb.frame_caption_argv(Path("/llama"), Path("/m.gguf"), Path("/mm.gguf"), Path("/f.jpg"))
    assert one[one.index("--image") + 1] == "/f.jpg"
    assert one[one.index("-c") + 1] == "2048"


def test_jpeg_write_and_ffprobe_argv():
    argv = lb.jpeg_write_argv("ffmpeg", Path("/o.jpg"), width=32, height=16)
    assert argv[:6] == ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo"]
    assert argv[argv.index("-s") + 1] == "32x16"
    assert argv[-1] == "/o.jpg"
    probe = lb.ffprobe_duration_argv("ffprobe", Path("/v.mp4"))
    assert probe[-3:] == ["-of", "default=nw=1:nk=1", "/v.mp4"]


def test_parse_mtmd_stdout_strips_ansi_and_prompt_echo():
    raw = "\x1b[1mDescribe what you see.\x1b[0m  A black puppy.  "
    assert lb.parse_mtmd_stdout(raw) == "Describe what you see. A black puppy."
    assert lb.parse_mtmd_stdout(raw, prompt="Describe what you see.") == "A black puppy."


# ── LocalVideoDuplexer: push/pull state machine (fake captions) ─────────────


def test_push_frame_emits_motion_and_batched_captions():
    frames = [
        Frame(data=b"\x00" * (8 * 8 * 3), width=8, height=8, ts=0.0, meta={"path": "/fake/a.jpg"}),
        Frame(data=b"\xff" * (8 * 8 * 3), width=8, height=8, ts=1.0, meta={"path": "/fake/b.jpg"}),
    ]
    seen_paths: list[str] = []

    def caption_fn(path, prompt):
        seen_paths.append(str(path))
        return f"caption of {Path(path).name}"

    duplexer = lb.LocalVideoDuplexer(caption_fn=caption_fn, caption_batch=2, caption_enabled=True)

    async def main():
        await duplexer.open()
        for frame in frames:
            await duplexer.push_frame(frame)
        assert await duplexer.flush_captions() == 2
        await duplexer.close()
        return [event async for event in duplexer.pull()]

    events = RUN(main())
    kinds = [e.kind for e in events]
    assert kinds == ["motion", "caption", "caption"]
    assert events[0].meta["energy"] == 1.0
    assert [e.text for e in events[1:]] == ["caption of a.jpg", "caption of b.jpg"]
    assert seen_paths == ["/fake/a.jpg", "/fake/b.jpg"]
    assert duplexer.stats["captions"] == 2 and duplexer.stats["motion_events"] == 1


def test_auto_batching_and_summary_hook_emit_events():
    summaries: list[list[str]] = []

    def summary_hook(texts):
        summaries.append(list(texts))
        return "rolling summary"

    duplexer = lb.LocalVideoDuplexer(
        caption_fn=lambda path, prompt: f"cap {Path(path).stem}",
        caption_batch=2,
        summary_hook=summary_hook,
        summary_window=2,
    )

    async def main():
        await duplexer.open()
        for idx in range(2):
            await duplexer.push_frame(
                Frame(data=bytes([idx]) * (8 * 8 * 3), width=8, height=8, ts=idx, meta={"path": f"/fake/f{idx}.jpg"})
            )
        for _ in range(200):  # let the auto-scheduled caption task run
            if duplexer.stats["summaries"]:
                break
            await asyncio.sleep(0.01)
        await duplexer.close()
        return [event async for event in duplexer.pull()]

    events = RUN(main())
    assert summaries == [["cap f0", "cap f1"]]
    assert [e.kind for e in events] == ["motion", "caption", "caption", "summary"]
    assert events[-1].text == "rolling summary"
    assert events[-1].meta == {"captions": 2}


def test_caption_error_becomes_error_event_not_crash():
    def bad_caption(path, prompt):
        raise RuntimeError("captioner exploded")

    duplexer = lb.LocalVideoDuplexer(caption_fn=bad_caption, caption_batch=1)
    frame = Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4, ts=0.0, meta={"path": "/fake/x.jpg"})

    async def main():
        await duplexer.open()
        await duplexer.push_frame(frame)
        await duplexer.flush_captions()
        await duplexer.close()
        return [event async for event in duplexer.pull()]

    events = RUN(main())
    assert [e.kind for e in events] == ["error"]
    assert "captioner exploded" in events[0].text
    assert duplexer.stats["errors"] == 1


def test_push_after_close_raises_and_pull_drains():
    duplexer = lb.LocalVideoDuplexer(caption_enabled=False)

    async def main():
        await duplexer.open()
        await duplexer.push_frame(Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4))
        await duplexer.close()
        with pytest.raises(VideoError, match="closed"):
            await duplexer.push_frame(Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4))

    RUN(main())


# ── LocalVideoDuplexer.ingest_video ─────────────────────────────────────────


def _patch_frames(monkeypatch, duplexer, files: list[Path]):
    async def fake_extract(video, out_dir, *, fps, width):
        return files

    monkeypatch.setattr(duplexer, "_extract_frames", fake_extract)


def test_ingest_frames_mode_events_with_timestamps(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    frames = []
    for idx in range(3):
        path = tmp_path / f"frame_{idx}.jpg"
        path.write_bytes(b"\xff\xd8\xff")
        frames.append(path)

    duplexer = lb.LocalVideoDuplexer(caption_fn=lambda path, prompt: f"caption {Path(path).name}", mode="frames")
    _patch_frames(monkeypatch, duplexer, frames)

    async def main():
        return [event async for event in duplexer.ingest_video(video, fps=2.0, mode="frames")]

    events = RUN(main())
    assert [e.kind for e in events] == ["caption", "caption", "caption"]
    assert [e.ts for e in events] == [0.0, 0.5, 1.0]
    assert events[0].meta["mode"] == "frames"
    assert duplexer.stats["frames_sampled"] == 3


def test_ingest_frames_partial_failure_keeps_going(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    frames = []
    for idx in range(3):
        path = tmp_path / f"frame_{idx}.jpg"
        path.write_bytes(b"\xff\xd8\xff")
        frames.append(path)

    def caption_fn(path, prompt):
        if "frame_1" in str(path):
            raise RuntimeError("bad frame")
        return f"ok {Path(path).name}"

    duplexer = lb.LocalVideoDuplexer(caption_fn=caption_fn, mode="frames")
    _patch_frames(monkeypatch, duplexer, frames)

    async def main():
        return [event async for event in duplexer.ingest_video(video, mode="frames")]

    events = RUN(main())
    assert [e.kind for e in events] == ["caption", "error", "caption"]


def test_ingest_all_frames_failing_raises(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")
    duplexer = lb.LocalVideoDuplexer(caption_fn=lambda p, prompt: (_ for _ in ()).throw(RuntimeError("nope")), mode="frames")
    _patch_frames(monkeypatch, duplexer, [frame])

    async def main():
        return [event async for event in duplexer.ingest_video(video, mode="frames")]

    with pytest.raises(VideoError, match="all 1 frame captions failed"):
        RUN(main())


def test_ingest_native_mode_argv_and_single_event(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    calls: list[dict] = []

    async def fake_run(argv, *, timeout=150.0, stdin=None, env=None, nice=True):
        calls.append({"argv": [str(a) for a in argv], "timeout": timeout, "env": env})
        return lb.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="\x1b[0mIn the video a black puppy plays.", stderr="")

    monkeypatch.setattr(lb, "run_command", fake_run)
    duplexer = lb.LocalVideoDuplexer(root="/root", llama_dir="/llama", model="/m.gguf", mmproj="/p.gguf", mode="native")

    async def main():
        return [event async for event in duplexer.ingest_video(video, mode="native", fps=2)]

    events = RUN(main())
    assert [e.kind for e in events] == ["caption"]
    assert events[0].text == "In the video a black puppy plays."
    assert events[0].meta["mode"] == "native"
    call = calls[0]
    assert call["argv"][call["argv"].index("--video") + 1] == str(video)
    assert call["argv"][call["argv"].index("--video-fps") + 1] == "2"
    assert call["argv"][call["argv"].index("-c") + 1] == "8192"
    assert call["env"]["VK_DRIVER_FILES"].endswith("nvidia_icd_egl.json")


def test_ingest_native_explicit_failure_yields_error_event(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")

    async def failing_run(argv, *, timeout=150.0, stdin=None, env=None, nice=True):
        return lb.CommandResult(argv=[str(a) for a in argv], returncode=1, stdout="", stderr="failed to find a memory slot")

    monkeypatch.setattr(lb, "run_command", failing_run)
    duplexer = lb.LocalVideoDuplexer(mode="native")

    async def main():
        return [event async for event in duplexer.ingest_video(video, mode="native")]

    events = RUN(main())
    assert [e.kind for e in events] == ["error"]
    assert "memory slot" in events[0].text


def test_ingest_auto_prefers_native_for_short_clips_and_falls_back(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")

    # short clip → native; assert the frame loop is never touched
    duplexer = lb.LocalVideoDuplexer(mode="auto", native_max_frames=8)

    async def fake_probe(path):
        return 2.0

    async def fake_extract(video_, out_dir, *, fps, width):
        raise AssertionError("frame loop must not run when native succeeds")

    async def ok_run(argv, *, timeout=150.0, stdin=None, env=None, nice=True):
        return lb.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="A black puppy.", stderr="")

    monkeypatch.setattr(duplexer, "_probe_duration", fake_probe)
    monkeypatch.setattr(duplexer, "_extract_frames", fake_extract)
    monkeypatch.setattr(lb, "run_command", ok_run)

    async def main():
        return [event async for event in duplexer.ingest_video(video, fps=2)]

    events = RUN(main())
    assert events[0].text == "A black puppy." and events[0].meta["mode"] == "native"

    # long clip → frames; native is not even attempted
    duplexer2 = lb.LocalVideoDuplexer(mode="auto", native_max_frames=8, caption_fn=lambda p, prompt: "frame caption")

    async def long_probe(path):
        return 60.0

    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")

    async def fake_extract2(video_, out_dir, *, fps, width):
        return [frame]

    async def boom_run(argv, *, timeout=150.0, stdin=None, env=None, nice=True):
        raise AssertionError("native must not run for long clips in auto mode")

    monkeypatch.setattr(duplexer2, "_probe_duration", long_probe)
    monkeypatch.setattr(duplexer2, "_extract_frames", fake_extract2)
    monkeypatch.setattr(lb, "run_command", boom_run)

    async def main2():
        return [event async for event in duplexer2.ingest_video(video, fps=2)]

    events2 = RUN(main2())
    assert events2[0].text == "frame caption" and events2[0].meta["mode"] == "frames"

    # native failure in auto mode → fall back to the frame loop
    duplexer3 = lb.LocalVideoDuplexer(mode="auto", caption_fn=lambda p, prompt: "fallback caption")

    async def short_probe(path):
        return 2.0

    async def failing_run(argv, *, timeout=150.0, stdin=None, env=None, nice=True):
        return lb.CommandResult(argv=[str(a) for a in argv], returncode=1, stdout="", stderr="boom")

    async def fake_extract3(video_, out_dir, *, fps, width):
        return [frame]

    monkeypatch.setattr(duplexer3, "_probe_duration", short_probe)
    monkeypatch.setattr(duplexer3, "_extract_frames", fake_extract3)
    monkeypatch.setattr(lb, "run_command", failing_run)

    async def main3():
        return [event async for event in duplexer3.ingest_video(video, fps=2)]

    events3 = RUN(main3())
    assert events3[0].text == "fallback caption" and events3[0].meta["mode"] == "frames"


def test_ingest_summary_hook_and_deliver_flag(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff")
    duplexer = lb.LocalVideoDuplexer(
        caption_fn=lambda p, prompt: "one caption",
        mode="frames",
        summary_hook=lambda texts: "sum: " + "|".join(texts),
    )
    _patch_frames(monkeypatch, duplexer, [frame])

    async def main():
        events = [event async for event in duplexer.ingest_video(video, mode="frames", deliver=True)]
        await duplexer.close()
        pulled = [event async for event in duplexer.pull()]
        return events, pulled

    events, pulled = RUN(main())
    assert [e.kind for e in events] == ["caption", "summary"]
    assert events[-1].text == "sum: one caption"
    assert [e.kind for e in pulled] == ["caption", "summary"]  # deliver=True mirrored them


def test_native_caption_accepts_output_after_teardown_crash(monkeypatch, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")

    async def crashy_run(argv, *, timeout=150.0, stdin=None, env=None, nice=True):
        return lb.CommandResult(
            argv=[str(a) for a in argv], returncode=-11,
            stdout="In the video a black puppy plays.", stderr="",
        )

    monkeypatch.setattr(lb, "run_command", crashy_run)
    duplexer = lb.LocalVideoDuplexer(mode="native")

    async def main():
        return [event async for event in duplexer.ingest_video(video, mode="native")]

    events = RUN(main())
    assert [e.kind for e in events] == ["caption"]
    assert events[0].text == "In the video a black puppy plays."


def test_ingest_missing_video_raises():
    duplexer = lb.LocalVideoDuplexer()

    async def main():
        return [event async for event in duplexer.ingest_video("/no/such/clip.mp4")]

    with pytest.raises(VideoError, match="video not found"):
        RUN(main())


# ── attach_track / frame_from_livekit (fake livekit module) ─────────────────


class _FakeRtcFrame:
    def __init__(self, width, height, payload: bytes):
        from livekit import rtc  # fake module injected by the test

        self.width, self.height = width, height
        self.type = rtc.VideoBufferType.RGB24
        self.data = memoryview(payload)
        self.converted = False

    def convert(self, target):  # pragma: no cover - not needed when already RGB24
        self.converted = True
        return self


def _install_fake_livekit(events: list):
    """Fake livekit(+rtc) module: VideoStream yields the given frame events."""
    livekit = pytypes.ModuleType("livekit")
    rtc = pytypes.ModuleType("livekit.rtc")

    class VideoBufferType:
        RGB24 = 4

    class VideoStream:
        def __init__(self, track, capacity=0, **kwargs):
            self.track = track
            self.closed = False

        def __aiter__(self):
            self._it = iter(events)
            return self

        async def __anext__(self):
            await asyncio.sleep(0.001)  # a real stream awaits I/O; give other tasks a turn
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

        async def aclose(self):
            self.closed = True

    rtc.VideoBufferType = VideoBufferType
    rtc.VideoStream = VideoStream
    livekit.rtc = rtc
    sys.modules["livekit"] = livekit
    sys.modules["livekit.rtc"] = rtc
    return livekit, rtc


def _uninstall_fake_livekit():
    sys.modules.pop("livekit", None)
    sys.modules.pop("livekit.rtc", None)


def test_frame_from_livekit_conversion():
    payload = b"\x01" * (8 * 8 * 3)
    _install_fake_livekit([])
    try:
        frame = lb.frame_from_livekit(_FakeRtcFrame(8, 8, payload), ts=3.0)
        assert frame.format == "rgb24" and frame.width == 8 and frame.ts == 3.0
        assert bytes(frame.data) == payload
        assert frame.meta["source"] == "livekit"
    finally:
        _uninstall_fake_livekit()


def test_attach_track_subsamples_and_pushes_frames():
    payload = b"\x11" * (8 * 8 * 3)
    _install_fake_livekit([])
    try:
        frame_events = [pytypes.SimpleNamespace(frame=_FakeRtcFrame(8, 8, payload)) for _ in range(3)]
        _install_fake_livekit(frame_events)
        captions: list[str] = []
        duplexer = lb.LocalVideoDuplexer(
            caption_fn=lambda path, prompt: captions.append(str(path)) or "live caption",
            caption_batch=1,
            fps=0,  # 0 = push every frame (no sub-sampling)
        )

        async def main():
            await duplexer.open()
            await duplexer.attach_track(object())  # the fake stream ignores the track
            await duplexer.flush_captions()
            await duplexer.close()
            return [event async for event in duplexer.pull()]

        events = RUN(main())
        kinds = [e.kind for e in events]
        assert kinds.count("caption") == 3
        assert kinds.count("motion") == 2
        assert duplexer.stats["frames_in"] == 3
        assert len(captions) == 3
    finally:
        _uninstall_fake_livekit()


def test_attach_track_honours_stop_event():
    payload = b"\x22" * (8 * 8 * 3)
    _install_fake_livekit([])
    try:
        frame_events = [pytypes.SimpleNamespace(frame=_FakeRtcFrame(8, 8, payload)) for _ in range(4)]
        _install_fake_livekit(frame_events)
        duplexer = lb.LocalVideoDuplexer(caption_enabled=False, fps=0)
        stop = asyncio.Event()

        async def main():
            await duplexer.open()
            await duplexer.attach_track(object(), stop_event=stop)

        async def stop_soon():
            # let attach_track consume a frame or two first, then stop
            for _ in range(200):
                if duplexer.stats["frames_in"] >= 1:
                    break
                await asyncio.sleep(0.001)
            stop.set()

        async def runner():
            task = asyncio.ensure_future(main())
            await stop_soon()
            await asyncio.wait_for(task, timeout=5)

        RUN(runner())
        assert 1 <= duplexer.stats["frames_in"] < 4  # stopped mid-stream, not at the first frame
    finally:
        _uninstall_fake_livekit()


# ── publish seam (fake livekit module) ──────────────────────────────────────


class _FakeStats:
    def __init__(self, type_, **fields):
        self.type = type_
        for key, value in fields.items():
            setattr(self, key, value)


def _install_fake_livekit_publish():
    livekit = pytypes.ModuleType("livekit")
    rtc = pytypes.ModuleType("livekit.rtc")
    calls: dict = {"capture": [], "published": [], "unpublished": [], "disconnected": False}

    class VideoBufferType:
        RGB24 = 4

    class VideoFrame:
        def __init__(self, width, height, type_, data):
            calls.setdefault("frames", []).append((width, height, type_, bytes(data)))
            self.width, self.height, self.data = width, height, data

    class VideoSource:
        def __init__(self, width, height, **kwargs):
            calls["source"] = (width, height)

        async def capture_frame(self, frame, *, timestamp_us=0):
            calls["capture"].append((timestamp_us, len(frame.data)))

    class LocalVideoTrack:
        def __init__(self, name):
            self.name = name

        @classmethod
        def create_video_track(cls, name, source):
            calls["track_name"] = name
            return cls(name)

        async def get_stats(self):
            return [
                _FakeStats("candidate-pair", state="SUCCEEDED", nominated=True, packets_sent=7, bytes_sent=900),
                _FakeStats("outbound-rtp", packets_sent=5, bytes_sent=500, packets_received=0),
            ]

    class TrackSource:
        SOURCE_CAMERA = 1
        SOURCE_MICROPHONE = 2
        SOURCE_SCREEN_SHARE = 3

    class TrackPublishOptions:
        def __init__(self):
            self.source = None

    class Participant:
        def __init__(self):
            self.sid = "PA_test"

        async def publish_track(self, track, options):
            calls["published"].append((track.name, options.source))
            return pytypes.SimpleNamespace(sid="TR_test")

        async def unpublish_track(self, sid):
            calls["unpublished"].append(sid)

    class Room:
        def __init__(self):
            self.local_participant = Participant()

        async def connect(self, url, token):
            calls["connect"] = (url, token)

        async def disconnect(self):
            calls["disconnected"] = True

    rtc.VideoBufferType = VideoBufferType
    rtc.VideoFrame = VideoFrame
    rtc.VideoSource = VideoSource
    rtc.LocalVideoTrack = LocalVideoTrack
    rtc.TrackSource = TrackSource
    rtc.TrackPublishOptions = TrackPublishOptions
    rtc.Room = Room
    livekit.rtc = rtc
    sys.modules["livekit"] = livekit
    sys.modules["livekit.rtc"] = rtc
    return calls


def test_publish_frames_livekit_happy_path():
    from fluxer.video.publish import publish_frames_livekit

    calls = _install_fake_livekit_publish()
    try:
        frames = [Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4, ts=idx / 10.0) for idx in range(3)]
        result = RUN(publish_frames_livekit("wss://endpoint", "token-123", frames, fps=200, name="probe-track"))

        assert calls["connect"] == ("wss://endpoint", "token-123")
        assert calls["source"] == (4, 4)
        assert calls["track_name"] == "probe-track"
        assert calls["published"] == [("probe-track", 1)]  # SOURCE_CAMERA
        assert len(calls["capture"]) == 3
        assert calls["capture"][0][0] == 0 and calls["capture"][1][0] == 100_000
        assert calls["unpublished"] == ["TR_test"] and calls["disconnected"] is True
        assert result["frames_pushed"] == 3 and result["frames_total"] == 3
        assert result["stopped"] == "completed"
        assert result["stats"]["outbound_rtp_totals"] == {"packets_sent": 5, "bytes_sent": 500, "packets_received": 0}
        assert result["stats"]["types"] == {"candidate-pair": 1, "outbound-rtp": 1}
    finally:
        _uninstall_fake_livekit()


def test_publish_frames_livekit_stop_event_and_validation():
    from fluxer.video.publish import publish_frames_livekit, summarize_track_stats

    calls = _install_fake_livekit_publish()
    try:
        frames = [Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4) for _ in range(3)]
        stop = asyncio.Event()

        async def main():
            stop.set()  # abort before the first frame
            return await publish_frames_livekit("wss://x", "t", frames, fps=200, stop_event=stop)

        result = RUN(main())
        assert result["frames_pushed"] == 0 and result["stopped"] == "stop_event"
        assert calls["unpublished"] == ["TR_test"]  # cleanup still ran

        with pytest.raises(VideoError, match="uniform frames"):
            RUN(
                publish_frames_livekit(
                    "wss://x", "t",
                    [Frame(data=b"\x00" * (4 * 4 * 3), width=4, height=4), Frame(data=b"\x00" * (8 * 8 * 3), width=8, height=8)],
                    fps=200,
                )
            )
        with pytest.raises(VideoError, match="at least one frame"):
            RUN(publish_frames_livekit("wss://x", "t", []))
    finally:
        _uninstall_fake_livekit()

    summary = summarize_track_stats([_FakeStats("outbound-rtp", packets_sent=3, bytes_sent=30)])
    assert summary["count"] == 1 and summary["outbound_rtp_totals"]["packets_sent"] == 3


def test_publish_rejects_unsupported_frame_format():
    from fluxer.video.publish import publish_frames_livekit

    calls = _install_fake_livekit_publish()
    try:
        with pytest.raises(VideoError, match="unsupported frame format"):
            RUN(publish_frames_livekit("wss://x", "t", [Frame(data=b"jpegbytes", width=2, height=2, format="jpeg")], fps=200))
    finally:
        _uninstall_fake_livekit()


# ── renderers (mocked ffmpeg seam) ──────────────────────────────────────────


def test_card_video_writes_textfiles_and_argv(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_run_ffmpeg(argv, *, stdin=None, timeout=300.0):
        calls.append({"argv": [str(a) for a in argv], "stdin": stdin, "timeout": timeout})
        return render.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(render, "run_ffmpeg", fake_run_ffmpeg)
    out = tmp_path / "card.mp4"
    text_dir = tmp_path / "texts"
    lines = ["Line one: it's fine, 100%", "Line two [] with, weird: chars"]

    async def main():
        return await render.card_video(lines, out, text_dir=text_dir, duration_per_line=1.5, gen_backend=None)

    result = RUN(main())
    assert result == out
    argv = calls[0]["argv"]
    assert argv[argv.index("-f") + 1] == "lavfi"
    assert "color=c=0x101418:s=640x360:d=3.000:r=24" in argv[argv.index("-i") + 1]
    vf = argv[argv.index("-vf") + 1]
    assert vf.count("drawtext=") == 2
    assert "textfile=" in vf and "expansion=none" in vf
    assert "enable='between(t,0.000,1.500)'" in vf
    assert "enable='between(t,1.500,3.000)'" in vf
    # the point of textfile=: special characters never reach the filtergraph
    written = sorted(text_dir.glob("card-*.txt"))
    assert [p.read_text() for p in written] == lines
    assert "it's" not in vf and "100%" not in vf


def test_annotate_video_windows_and_textfiles(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_run_ffmpeg(argv, *, stdin=None, timeout=300.0):
        calls.append({"argv": [str(a) for a in argv]})
        return render.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")

    async def fake_probe_size(src, *, ffprobe="ffprobe", timeout=30.0):
        return (320, 240)

    monkeypatch.setattr(render, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(render, "probe_size", fake_probe_size)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "annotated.mp4"
    events = [
        VideoEvent(1.0, "caption", "second caption"),
        VideoEvent(0.0, "caption", "first caption"),
        VideoEvent(0.5, "motion", None),  # ignored
    ]

    async def main():
        return await render.annotate_video(src, events, out, text_dir=tmp_path / "texts")

    assert RUN(main()) == out
    argv = calls[0]["argv"]
    vf = argv[argv.index("-vf") + 1]
    assert "enable='between(t,0.000,1.000)'" in vf  # sorted by ts
    assert "enable='gte(t,1.000)'" in vf
    assert "box=1" in vf and "boxcolor=black@0.55" in vf
    written = sorted((tmp_path / "texts").glob("ann-*.txt"))
    assert [p.read_text() for p in written] == ["first caption", "second caption"]
    assert argv[argv.index("-i") + 1] == str(src)


def test_annotate_wraps_long_captions_to_frame_width(monkeypatch, tmp_path):
    async def fake_run_ffmpeg(argv, *, stdin=None, timeout=300.0):
        return render.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")

    async def fake_probe_size(src, *, ffprobe="ffprobe", timeout=30.0):
        return (320, 240)

    monkeypatch.setattr(render, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(render, "probe_size", fake_probe_size)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00")
    long_caption = "A black dog on a wooden deck, looking at the camera."

    async def main():
        return await render.annotate_video(
            src, [VideoEvent(0.0, "caption", long_caption)], tmp_path / "o.mp4", text_dir=tmp_path / "texts"
        )

    RUN(main())
    wrapped = (tmp_path / "texts/ann-0000.txt").read_text()
    assert "\n" in wrapped  # multi-line drawtext instead of a cropped single line
    assert "…" not in wrapped  # fits within max_lines
    max_chars = int((320 - 40) / (0.55 * 24))  # frame width minus box padding
    assert all(len(line) <= max_chars for line in wrapped.splitlines())
    assert wrapped.replace("\n", " ") == long_caption


def test_wrap_for_width_ellipsizes_over_max_lines():
    text = " ".join(["word"] * 200)
    wrapped = render.wrap_for_width(text, width=320, fontsize=24, max_lines=3)
    lines = wrapped.splitlines()
    assert len(lines) == 3 and lines[-1].endswith("…")
    assert render.wrap_for_width("short", width=640, fontsize=28) == "short"
    assert render.wrap_for_width("", width=640, fontsize=28) == ""


def test_annotate_video_needs_caption_events(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00")
    with pytest.raises(VideoError, match="caption event"):
        RUN(render.annotate_video(src, [VideoEvent(0.0, "motion")], tmp_path / "o.mp4"))


def test_relay_frames_streams_chunks_to_ffmpeg(monkeypatch, tmp_path):
    captured: dict = {}

    async def fake_stream(argv, chunks, *, timeout=300.0):
        frames = [chunk async for chunk in chunks]
        captured.update(argv=[str(a) for a in argv], frames=frames, timeout=timeout)
        return render.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(render, "stream_to_ffmpeg", fake_stream)
    frames = [Frame(data=bytes([idx]) * (4 * 4 * 3), width=4, height=4) for idx in range(3)]

    async def main():
        return await render.relay_frames(frames, tmp_path / "out.mp4", fps=2)

    assert RUN(main()) == tmp_path / "out.mp4"
    argv = captured["argv"]
    assert argv[argv.index("-f") + 1] == "rawvideo"
    assert argv[argv.index("-pix_fmt") + 1] == "rgb24"
    assert argv[argv.index("-s") + 1] == "4x4"
    assert argv[argv.index("-r") + 1] == "2"
    assert argv[-1] == str(tmp_path / "out.mp4")
    assert captured["frames"] == [bytes([0]) * 48, bytes([1]) * 48, bytes([2]) * 48]


def test_relay_frames_rejects_non_uniform(monkeypatch, tmp_path):
    async def fake_stream(argv, chunks, *, timeout=300.0):
        async for _chunk in chunks:
            pass
        return render.CommandResult(argv=[str(a) for a in argv], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(render, "stream_to_ffmpeg", fake_stream)
    frames = [Frame(data=b"\x00" * 48, width=4, height=4), Frame(data=b"\x00" * 192, width=8, height=8)]
    with pytest.raises(VideoError, match="uniform frames"):
        RUN(render.relay_frames(frames, tmp_path / "o.mp4"))
    with pytest.raises(VideoError, match="at least one frame"):
        RUN(render.relay_frames([], tmp_path / "o.mp4"))


def test_gen_backend_seam_is_documented_but_not_built(tmp_path):
    with pytest.raises(VideoError, match="seam only"):
        RUN(render.card_video(["x"], tmp_path / "o.mp4", gen_backend="future-model"))
    assert render.GEN_BACKENDS == ()


def test_run_ffmpeg_real_subprocess_success_and_failure():
    ok = RUN(render.run_ffmpeg(["true"], timeout=10))
    assert ok.ok
    with pytest.raises(VideoError, match="rc=1"):
        RUN(render.run_ffmpeg(["false"], timeout=10))
    # a missing binary under `nice` surfaces as rc=127 (nice could not exec it)…
    with pytest.raises(VideoError, match="rc=127"):
        RUN(render.run_ffmpeg(["/definitely/not/ffmpeg"], timeout=5))
    # …and without the nice prefix it is a spawn failure
    with pytest.raises(VideoError, match="failed to spawn"):
        RUN(render.run_ffmpeg(["/definitely/not/ffmpeg"], timeout=5, nice=False))


def test_video_package_imports_without_livekit():
    code = (
        "import sys, importlib;"
        f"sys.path.insert(0, {str(PLUGIN_SRC / 'fluxer')!r});"
        "import video, video.duplex, video.local_backend, video.render, video.publish;"
        "assert 'livekit' not in sys.modules, 'livekit imported eagerly';"
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
