"""Local (all-GGUF, no torch) video duplexer (spec §6 W4).

IN
    * ``ingest_video(path, fps=2)`` — files: either the **native** llama.cpp
      ``--video`` path (2 s clip @ 2 fps needed ``-c 8192``; one model load for
      the whole clip — the cheap path, defaults for short clips) or the
      frame-loop fallback (ffmpeg sampling → SmolVLM2 caption per frame, with
      per-caption timestamps).  ``mode="auto"`` picks native for short clips.
    * ``push_frame(frame, ts)`` / ``attach_track(track)`` — live frames.  The
      LiveKit import happens **only inside** ``attach_track``; frames go into a
      bounded ring buffer and are captioned in periodic batches.

OUT
    * ``pull()`` — async iterator of :class:`~fluxer.video.duplex.VideoEvent`
      (caption / motion / summary / error).

The captions come from the installed SmolVLM2 GGUF via the omni registry
backend ``local.smolvlm``; ``caption_fn`` can be injected for tests or for a
future unified model.  The LiveKit track path is implemented but intentionally
**not live-tested tonight** (no voice-channel joins during the C4b run) — it
is unit-tested against a fake livekit module.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from .duplex import Frame, VideoDuplexer, VideoEvent, VideoError

logger = logging.getLogger(__name__)

__all__ = [
    "LocalVideoDuplexer",
    "CAPTION_PROMPT",
    "frame_sampling_argv",
    "native_video_argv",
    "frame_caption_argv",
    "jpeg_write_argv",
    "ffprobe_duration_argv",
    "parse_mtmd_stdout",
    "motion_energy",
    "frame_from_livekit",
    "run_command",
    "CommandResult",
]

CAPTION_PROMPT = "Describe what you see."
DEFAULT_FPS = 2.0
DEFAULT_WIDTH = 512
DEFAULT_MAX_FRAMES = 12
#: ``auto`` mode keeps the native path for clips whose sampled frame count fits
#: the verified ``-c 8192`` budget (2 s @ 2 fps worked; ~8 frames is the cap).
NATIVE_MAX_FRAMES = 8

_RUN_NICE: tuple[str, ...] = ("nice", "-n", "10")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _default_root() -> Path:
    return Path(os.environ.get("FLUXER_WORKSPACE", "/home/agent/workspace/fluxer"))


def apply_gpu_env(env: dict[str, str] | None = None, *, root: str | Path | None = None) -> dict[str, str]:
    """Mirror gpu/gpu-env.sh (Vulkan ICD + libEGL) for subprocess calls."""
    merged = dict(os.environ if env is None else env)
    gpu = Path(root or _default_root()) / "gpu"
    merged.setdefault("VK_DRIVER_FILES", str(gpu / "nvidia_icd_egl.json"))
    egl = str(gpu / "extract-egl/usr/lib/x86_64-linux-gnu")
    existing = merged.get("LD_LIBRARY_PATH")
    merged["LD_LIBRARY_PATH"] = f"{egl}:{existing}" if existing else egl
    return merged


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_command(
    argv: Sequence[str],
    *,
    timeout: float = 150.0,
    stdin: str | bytes | None = None,
    env: dict[str, str] | None = None,
    nice: bool = True,
) -> CommandResult:
    """Run ``argv`` (nice'd, timeout-bounded) capturing text output."""
    argv = [str(a) for a in argv]
    full = [*_RUN_NICE, *argv] if nice else list(argv)
    payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    try:
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise VideoError(f"failed to spawn {full[0]!r}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=payload), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise VideoError(f"command timed out after {timeout}s: {' '.join(argv)}") from None
    return CommandResult(
        argv=full,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=(out or b"").decode("utf-8", "replace"),
        stderr=(err or b"").decode("utf-8", "replace"),
    )


# ── pure argv builders (unit-tested; live runs exercise them too) ────────────


def _fmt_num(value: float | int) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def frame_sampling_argv(ffmpeg: str, video: Path, out_dir: Path, *, fps: float, width: int) -> list[str]:
    """ffmpeg frame sampling — the exact shape proven in scripts/caption_video.py."""
    return [
        str(ffmpeg), "-v", "error", "-y",
        "-i", str(video),
        "-vf", f"fps={_fmt_num(fps)},scale={int(width)}:-2",
        "-q:v", "3",
        str(Path(out_dir) / "frame_%05d.jpg"),
    ]


def native_video_argv(
    llama_dir: Path,
    model: Path,
    mmproj: Path,
    video: Path,
    *,
    fps: float = 2.0,
    prompt: str = CAPTION_PROMPT,
    n_predict: int = 48,
    ctx: int = 8192,  # §3.4: video ctx must fit the sampled frames (2048 died)
    ub: int = 64,
    b: int = 128,
) -> list[str]:
    """llama-mtmd-cli with the native ``--video`` flag (the cheap file path)."""
    return [
        str(Path(llama_dir) / "llama-mtmd-cli"),
        "-m", str(model), "--mmproj", str(mmproj),
        "--video", str(video), "--video-fps", _fmt_num(fps),
        "-p", prompt,
        "-n", str(n_predict), "-ub", str(ub), "-b", str(b), "-c", str(ctx),
    ]


def frame_caption_argv(
    llama_dir: Path,
    model: Path,
    mmproj: Path,
    image: Path,
    *,
    prompt: str = CAPTION_PROMPT,
    n_predict: int = 64,
    ctx: int = 2048,
    ub: int = 64,
    b: int = 128,
) -> list[str]:
    """One SmolVLM2 caption for one extracted frame file."""
    return [
        str(Path(llama_dir) / "llama-mtmd-cli"),
        "-m", str(model), "--mmproj", str(mmproj),
        "--image", str(image),
        "-p", prompt,
        "-n", str(n_predict), "-ub", str(ub), "-b", str(b), "-c", str(ctx),
    ]


def jpeg_write_argv(ffmpeg: str, out: Path, *, width: int, height: int, pix_fmt: str = "rgb24") -> list[str]:
    """Encode one raw frame from stdin to a JPEG (used by the live path)."""
    return [
        str(ffmpeg), "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{int(width)}x{int(height)}",
        "-i", "-", "-frames:v", "1",
        str(out),
    ]


def ffprobe_duration_argv(ffprobe: str, video: Path) -> list[str]:
    return [
        str(ffprobe), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(video),
    ]


def parse_mtmd_stdout(stdout: str, *, prompt: str | None = None) -> str:
    """llama-mtmd-cli logs to stderr; the reply is stdout. Strip ANSI + prompt echo."""
    text = _ANSI_RE.sub("", stdout or "").strip()
    if prompt and prompt in text:
        text = text.rsplit(prompt, 1)[-1]
    return " ".join(text.split())


def motion_energy(prev: Frame, cur: Frame, *, stride: int = 64) -> float | None:
    """Cheap sampled mean-abs-difference (0..1) between two packed frames.

    Not a model — an honest local signal that something moved; returns None
    when the frames are not compatible (different size/format/non-bytes).
    """
    if prev.format != cur.format or prev.width != cur.width or prev.height != cur.height:
        return None
    if not isinstance(prev.data, (bytes, bytearray, memoryview)) or not isinstance(cur.data, (bytes, bytearray, memoryview)):
        return None
    n = min(len(prev.data), len(cur.data))
    stride = max(1, int(stride))
    total = count = 0
    a, b = prev.data, cur.data
    for i in range(0, n, stride):
        total += abs(int(a[i]) - int(b[i]))
        count += 1
    if not count:
        return None
    return total / count / 255.0


def _rtc() -> Any:
    """Lazy livekit import (only ever called from track/publish code)."""
    try:
        from livekit import rtc  # noqa: PLC0415 — deliberately lazy
    except ImportError as exc:  # pragma: no cover - livekit is installed on this box
        raise VideoError("livekit is required for track attach; install the 'livekit' package") from exc
    return rtc


def frame_from_livekit(frame: Any, *, ts: float | None = None) -> Frame:
    """Convert an ``rtc.VideoFrame`` into the seam :class:`Frame` (RGB24)."""
    rtc = _rtc()
    if getattr(frame, "type", None) != rtc.VideoBufferType.RGB24:
        frame = frame.convert(rtc.VideoBufferType.RGB24)
    data = frame.data
    if not isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data)
    return Frame(
        data=data,
        width=int(frame.width),
        height=int(frame.height),
        format="rgb24",
        ts=float(ts if ts is not None else 0.0),
        meta={"source": "livekit"},
    )


class LocalVideoDuplexer:
    """ffmpeg + SmolVLM2 video duplexer (file ingest and live frames)."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        llama_dir: str | Path | None = None,
        model: str | Path | None = None,
        mmproj: str | Path | None = None,
        prompt: str = CAPTION_PROMPT,
        fps: float = DEFAULT_FPS,
        width: int = DEFAULT_WIDTH,
        max_frames: int = DEFAULT_MAX_FRAMES,
        native_max_frames: int = NATIVE_MAX_FRAMES,
        mode: str = "auto",
        ring_size: int = 16,
        caption_batch: int = 4,
        caption_enabled: bool = True,
        summary_hook: Callable[[list[str]], Any] | None = None,
        summary_window: int = 4,
        caption_fn: Callable[[Path, str], Awaitable[str] | str] | None = None,
        timeout: float = 150.0,
        motion_stride: int = 64,
    ) -> None:
        self.root = Path(root) if root is not None else _default_root()
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.llama_dir = Path(llama_dir) if llama_dir is not None else self.root / "gpu/tools/llama-b10903"
        self.model = Path(model) if model is not None else self.root / "models/smolvlm2-256m.gguf"
        self.mmproj = Path(mmproj) if mmproj is not None else self.root / "models/mmproj-smolvlm2.gguf"
        self.prompt = prompt
        self.fps = float(fps)
        self.width = int(width)
        self.max_frames = int(max_frames)
        self.native_max_frames = int(native_max_frames)
        self.mode = str(mode).lower()
        self.caption_batch = max(1, int(caption_batch))
        self.caption_enabled = bool(caption_enabled)
        self.summary_hook = summary_hook
        self.summary_window = max(1, int(summary_window))
        self.caption_fn = caption_fn
        self.timeout = float(timeout)
        self.motion_stride = int(motion_stride)

        self._queue: asyncio.Queue[VideoEvent | None] | None = None
        self._opened = False
        self._closed = False
        self._ring: deque[Frame] = deque(maxlen=max(1, int(ring_size)))
        self._pending: deque[Frame] = deque()
        self._caption_task: asyncio.Task | None = None
        self._caption_lock = asyncio.Lock()
        self._recent_captions: deque[str] = deque(maxlen=self.summary_window)
        self._last_frame: Frame | None = None
        self._stats: dict[str, int] = {
            "frames_in": 0,
            "motion_events": 0,
            "captions": 0,
            "summaries": 0,
            "errors": 0,
            "dropped_events": 0,
            "frames_sampled": 0,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _ensure_open(self) -> None:
        if self._closed:
            raise VideoError("duplexer is closed")
        if not self._opened:
            self._queue = asyncio.Queue(maxsize=1024)
            self._opened = True

    async def open(self) -> None:
        self._ensure_open()

    async def close(self, *, flush: bool = False) -> None:
        """Stop captioning and end ``pull()``. ``flush=True`` drains pending captions first."""
        if self._closed:
            return
        if flush:
            try:
                await asyncio.wait_for(self.flush_captions(), timeout=self.timeout)
            except Exception:  # noqa: BLE001 - close must not raise for a flush attempt
                pass
        self._closed = True
        task = self._caption_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._queue is not None:
            await self._queue.put(None)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ── OUT: event stream ────────────────────────────────────────────────────

    def _emit(self, event: VideoEvent) -> None:
        self._ensure_open()
        assert self._queue is not None
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._stats["dropped_events"] += 1
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            self._queue.put_nowait(event)

    async def pull(self) -> AsyncIterator[VideoEvent]:
        """Async iterator over output events (ends after :meth:`close`).

        Pulling a closed duplexer is allowed: buffered events drain first, then
        the iterator ends (the close sentinel is already queued).
        """
        if self._queue is None:
            self._ensure_open()
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    # ── IN: live frames ──────────────────────────────────────────────────────

    async def push_frame(self, frame: Frame, ts: float | None = None) -> None:
        """Push one live frame: motion event now, captioned in batches."""
        self._ensure_open()
        if ts is not None:
            frame.ts = float(ts)
        previous = self._last_frame
        if previous is not None:
            energy = motion_energy(previous, frame, stride=self.motion_stride)
            if energy is not None:
                self._stats["motion_events"] += 1
                self._emit(
                    VideoEvent(frame.ts, "motion", None, {"energy": round(float(energy), 5)})
                )
        self._last_frame = frame
        self._ring.append(frame)
        self._stats["frames_in"] += 1
        if self.caption_enabled:
            self._pending.append(frame)
            if len(self._pending) >= self.caption_batch:
                self._schedule_captioning()

    async def attach_track(self, track: Any, *, fps: float | None = None, capacity: int = 8, stop_event: Any = None) -> None:
        """Attach a LiveKit video track and feed frames through the seam.

        **Lazy LiveKit import happens only here** (and in ``publish``).  Frames
        are sub-sampled to ``fps`` and pushed via :meth:`push_frame` (bounded
        ring buffer + periodic caption batches downstream).

        NOT live-tested in wave 4 (no voice-channel joins during the C4b run);
        unit-tested against a fake ``livekit`` module.  Integration test is
        deferred to the voice wave (join → attach → captions in the transcript).
        """
        rtc = _rtc()
        target_fps = float(fps if fps is not None else self.fps)
        interval = (1.0 / target_fps) if target_fps > 0 else 0.0
        stream = rtc.VideoStream(track, capacity=int(capacity))
        t0 = time.monotonic()
        last_ts = -1e9
        try:
            async for event in stream:
                if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                    break
                now = time.monotonic() - t0
                if now - last_ts < interval:
                    continue
                last_ts = now
                await self.push_frame(frame_from_livekit(event.frame, ts=now), ts=now)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    def _schedule_captioning(self) -> None:
        if self._closed:
            return
        if self._caption_task is None or self._caption_task.done():
            self._caption_task = asyncio.create_task(self._caption_worker())

    async def _caption_worker(self) -> None:
        try:
            await self.flush_captions()
        except Exception as exc:  # noqa: BLE001 - a bad batch must not kill the stream
            self._stats["errors"] += 1
            ts = self._last_frame.ts if self._last_frame else 0.0
            self._emit(VideoEvent(ts, "error", str(exc)[:300], {"source": "caption_worker"}))

    async def flush_captions(self) -> int:
        """Caption every pending frame (in batches); returns the caption count."""
        done = 0
        async with self._caption_lock:
            while self._pending:
                batch = [self._pending.popleft() for _ in range(min(len(self._pending), self.caption_batch))]
                for frame in batch:
                    try:
                        path = (
                            Path(str(frame.meta["path"]))
                            if frame.meta.get("path")
                            else await self._write_jpeg(frame)
                        )
                        text = await self._caption_path(path, self.prompt)
                    except Exception as exc:  # noqa: BLE001
                        self._stats["errors"] += 1
                        self._emit(VideoEvent(frame.ts, "error", str(exc)[:300], {"source": "caption"}))
                        continue
                    self._stats["captions"] += 1
                    self._recent_captions.append(text)
                    self._emit(VideoEvent(frame.ts, "caption", text, {"source": "live"}))
                    done += 1
                await self._maybe_summary()
        return done

    async def _maybe_summary(self) -> VideoEvent | None:
        if self.summary_hook is None or len(self._recent_captions) < self.summary_window:
            return None
        texts = list(self._recent_captions)
        self._recent_captions.clear()
        ts = self._last_frame.ts if self._last_frame is not None else 0.0
        event = await self._summarize_texts(texts, ts)
        if event is not None:
            self._emit(event)
        return event

    async def _summarize_texts(self, texts: list[str], ts: float) -> VideoEvent | None:
        if self.summary_hook is None or not texts:
            return None
        result = self.summary_hook(list(texts))
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return None
        self._stats["summaries"] += 1
        return VideoEvent(ts, "summary", str(result), {"captions": len(texts)})

    # ── caption plumbing (monkeypatch-friendly seams for tests) ─────────────

    async def _caption_path(self, path: Path, prompt: str) -> str:
        fn = self.caption_fn or self._default_caption
        result = fn(Path(path), prompt)
        if inspect.isawaitable(result):
            result = await result
        return " ".join(str(result).split())

    async def _default_caption(self, path: Path, prompt: str) -> str:
        """Default captioner: the omni registry SmolVLM2 backend (lazy import)."""
        from ..omni.registry import SmolVLMBackend  # lazy: keep video import-light

        backend = SmolVLMBackend(
            root=self.root, llama_dir=self.llama_dir, model=self.model, mmproj=self.mmproj, timeout=self.timeout
        )
        return await backend.caption_file(path, prompt)

    async def _write_jpeg(self, frame: Frame) -> Path:
        """Persist a raw frame as a JPEG (ffmpeg rawvideo on stdin) for captioning."""
        out_dir = Path(tempfile.mkdtemp(prefix="fluxer-frames-"))
        out = out_dir / f"frame-{int(time.time() * 1000)}.jpg"
        argv = jpeg_write_argv(self.ffmpeg, out, width=frame.width, height=frame.height, pix_fmt=frame.format)
        data = frame.data if isinstance(frame.data, (bytes, bytearray, memoryview)) else bytes(frame.data)
        result = await run_command(argv, timeout=60.0, stdin=bytes(data))
        if not result.ok:
            raise VideoError(f"ffmpeg jpeg write rc={result.returncode}: {result.stderr.strip()[-200:]}")
        return out

    # ── IN: file ingest ──────────────────────────────────────────────────────

    async def ingest_video(
        self,
        video: str | Path,
        *,
        fps: float | None = None,
        mode: str | None = None,
        prompt: str | None = None,
        width: int | None = None,
        deliver: bool = False,
    ) -> AsyncIterator[VideoEvent]:
        """Caption a video file → event stream (native ``--video`` or frame loop).

        ``deliver=True`` also pushes every event into :meth:`pull` (off by
        default: file ingest is normally consumed as a turn, not as a stream).
        """
        video = Path(video)
        if not video.exists():
            raise VideoError(f"video not found: {video}")
        fps = float(fps if fps is not None else self.fps)
        requested_mode = str(mode if mode is not None else self.mode).lower()
        if requested_mode not in ("auto", "native", "frames"):
            raise VideoError(f"unknown ingest mode {requested_mode!r}")
        prompt = prompt or self.prompt
        width = int(width if width is not None else self.width)

        effective = requested_mode
        if effective == "auto":
            duration = await self._probe_duration(video)
            effective = "native" if duration and duration * fps <= self.native_max_frames else "frames"

        captions: list[VideoEvent] = []
        if effective == "native":
            try:
                caption = await self._caption_native(video, fps=fps, prompt=prompt)
            except VideoError as exc:
                if requested_mode == "native":
                    self._stats["errors"] += 1
                    event = VideoEvent(0.0, "error", str(exc)[:300], {"mode": "native", "video": str(video)})
                    if deliver:
                        self._emit(event)
                    yield event
                    return
                effective = "frames"  # auto fallback (native is the cheap path, not the only one)
            else:
                self._stats["captions"] += 1
                captions.append(
                    VideoEvent(0.0, "caption", caption, {"mode": "native", "video": str(video), "fps": fps})
                )

        if effective == "frames":
            captions.extend(await self._ingest_frames(video, fps=fps, prompt=prompt, width=width))

        for event in captions:
            if deliver:
                self._emit(event)
            yield event

        summary = await self._summarize_texts([e.text or "" for e in captions if e.kind == "caption"], captions[-1].ts if captions else 0.0)
        if summary is not None:
            if deliver:
                self._emit(summary)
            yield summary

    async def _caption_native(self, video: Path, *, fps: float, prompt: str) -> str:
        argv = native_video_argv(self.llama_dir, self.model, self.mmproj, video, fps=fps, prompt=prompt)
        result = await run_command(argv, timeout=self.timeout, env=apply_gpu_env(root=self.root))
        text = parse_mtmd_stdout(result.stdout, prompt=prompt)
        # Same teardown-flake tolerance as the omni backends: a complete caption
        # on stdout is accepted even when the process exited abnormally.
        if not result.ok and not text:
            raise VideoError(f"llama-mtmd-cli --video rc={result.returncode}: {result.stderr.strip()[-300:]}")
        if not result.ok:
            logger.warning(
                "native --video exited rc=%s after printing a caption (flaky teardown on this "
                "llama.cpp Vulkan build); output accepted",
                result.returncode,
            )
        if not text:
            raise VideoError("native --video path returned an empty caption")
        return text

    async def _ingest_frames(self, video: Path, *, fps: float, prompt: str, width: int) -> list[VideoEvent]:
        out_dir = Path(tempfile.mkdtemp(prefix="fluxer-frames-"))
        frames = await self._extract_frames(video, out_dir, fps=fps, width=width)
        self._stats["frames_sampled"] += len(frames)
        if not frames:
            raise VideoError(f"no frames extracted from {video}")
        events: list[VideoEvent] = []
        for idx, frame in enumerate(frames[: self.max_frames]):
            ts = round(idx / fps, 3)
            try:
                text = await self._caption_path(Path(frame), prompt)
            except Exception as exc:  # noqa: BLE001 - one bad frame is not fatal
                self._stats["errors"] += 1
                events.append(VideoEvent(ts, "error", str(exc)[:300], {"frame": Path(frame).name}))
                continue
            self._stats["captions"] += 1
            events.append(VideoEvent(ts, "caption", text, {"mode": "frames", "frame": Path(frame).name, "video": str(video)}))
        if not any(e.kind == "caption" for e in events):
            raise VideoError(f"all {len(events)} frame captions failed for {video}")
        return events

    async def _extract_frames(self, video: Path, out_dir: Path, *, fps: float, width: int) -> list[Path]:
        """ffmpeg frame sampling → sorted jpg paths (patched in unit tests)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        result = await run_command(
            frame_sampling_argv(self.ffmpeg, video, out_dir, fps=fps, width=width),
            timeout=180.0,
        )
        if not result.ok:
            raise VideoError(f"ffmpeg frame sampling rc={result.returncode}: {result.stderr.strip()[-300:]}")
        return sorted(out_dir.glob("frame_*.jpg"))

    async def _probe_duration(self, video: Path) -> float | None:
        """ffprobe duration in seconds (None when it cannot be determined)."""
        try:
            result = await run_command(ffprobe_duration_argv(self.ffprobe, video), timeout=30.0)
        except VideoError:
            return None
        if not result.ok:
            return None
        try:
            return float(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
