"""Local ffmpeg renderers — the video OUT path for hermes-omni, no new deps.

Three renderers, all driven by the system ffmpeg:

* :func:`card_video` — ``lines`` → a slideshow MP4 (drawtext cards, one line
  per slot, ``duration_per_line`` seconds each);
* :func:`annotate_video` — overlay caption events on a source clip, each one
  enabled for its timestamp window (``ts`` → next ``ts``);
* :func:`relay_frames` — a sequence of raw frames → MP4 (this is the piece the
  LiveKit publish path feeds from).

**Escaping:** drawtext's ``text=`` option is a quoting minefield (backslash
escaping inside single quotes breaks on apostrophes). These renderers therefore
write each text into its own file and use ``textfile=`` + ``expansion=none``;
only the sandboxed temp path reaches the filtergraph.  A font is
auto-discovered (DejaVu); when none is found ffmpeg's fontconfig fallback is
used.

**Future generative model seam:** ``gen_backend`` is accepted (and documented)
but not implemented — when a video generator that fits 6 GB appears, it should
implement the same ``card_video``/``annotate_video``/``relay_frames`` triad
instead of being smuggled into the ffmpeg path.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence

from .duplex import Frame, VideoError

__all__ = [
    "card_video",
    "card_video_argv",
    "annotate_video",
    "annotate_video_argv",
    "relay_frames",
    "relay_frames_argv",
    "write_text_files",
    "wrap_for_width",
    "probe_size",
    "find_font",
    "run_ffmpeg",
    "stream_to_ffmpeg",
    "GEN_BACKENDS",
]

RUN_NICE: tuple[str, ...] = ("nice", "-n", "10")

#: No generative video backends are implemented (seam only; docs in module header).
GEN_BACKENDS: tuple[str, ...] = ()

FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)

DEFAULT_BACKGROUND = "0x101418"
DEFAULT_FONTSIZE = 28
DEFAULT_FPS = 24


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def find_font() -> str | None:
    """First installed font from :data:`FONT_CANDIDATES` (None → fontconfig fallback)."""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def write_text_files(
    texts: Sequence[str], out_dir: Path, *, prefix: str = "text", collapse_newlines: bool = True
) -> list[Path]:
    """Write each text to its own file (drawtext ``textfile=``; one entry each).

    ``collapse_newlines=False`` keeps intentional line breaks produced by
    :func:`wrap_for_width` (multi-line drawtext for narrow frames).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for idx, text in enumerate(texts):
        path = out_dir / f"{prefix}-{idx:04d}.txt"
        raw = str(text).replace("\r", "")
        clean = " ".join(raw.split()) if collapse_newlines else "\n".join(line.strip() for line in raw.split("\n") if line.strip())
        path.write_text(clean, encoding="utf-8")
        paths.append(path)
    return paths


#: Rough glyph-width factor for DejaVu-class sans fonts (fraction of fontsize).
_CHAR_WIDTH_FACTOR = 0.55


def wrap_for_width(text: str, *, width: int, fontsize: int, max_lines: int = 3) -> str:
    """Wrap ``text`` so it fits a ``width``-px frame at ``fontsize`` (ellipsis on overflow).

    drawtext does not auto-wrap, and centered text wider than the frame is
    silently cropped (observed live on a 320 px clip) — so long captions become
    a few explicit lines (``textfile=`` keeps the newlines).
    """
    text = " ".join(str(text).split())
    if not text:
        return ""
    max_chars = max(12, int(width / (_CHAR_WIDTH_FACTOR * max(1, fontsize))))
    lines = textwrap.wrap(text, width=max_chars) or [text]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(4, max_chars - 1)].rstrip() + "…"
    return "\n".join(lines)


def ffprobe_size_argv(ffprobe: str, video: Path) -> list[str]:
    return [
        str(ffprobe), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(video),
    ]


async def probe_size(src: str | Path, *, ffprobe: str = "ffprobe", timeout: float = 30.0) -> tuple[int, int] | None:
    """Video (width, height) via ffprobe; None when it cannot be determined."""
    try:
        result = await run_ffmpeg(ffprobe_size_argv(ffprobe, Path(src)), timeout=timeout)
    except VideoError:
        return None
    for token in reversed(result.stdout.strip().splitlines()):
        match = re.match(r"^(\d+)x(\d+)", token.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _drawtext(
    text_file: Path,
    *,
    font: str | None,
    fontsize: int,
    x: str,
    y: str,
    enable: str,
    box: bool = False,
    boxcolor: str = "black@0.55",
) -> str:
    parts = []
    if font:
        parts.append(f"fontfile={font}")
    parts += [
        f"textfile={text_file}",
        "expansion=none",
        "fontcolor=white",
        f"fontsize={int(fontsize)}",
        f"x={x}",
        f"y={y}",
    ]
    if box:
        parts += ["box=1", f"boxcolor={boxcolor}", "boxborderw=10"]
    parts.append(f"enable={enable}")
    return "drawtext=" + ":".join(parts)


def card_video_argv(
    text_files: Sequence[Path],
    out: Path,
    *,
    width: int = 640,
    height: int = 360,
    duration_per_line: float = 2.0,
    fps: int = DEFAULT_FPS,
    font: str | None = None,
    fontsize: int = DEFAULT_FONTSIZE,
    background: str = DEFAULT_BACKGROUND,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """argv for the card slideshow (one text file per line)."""
    if not text_files:
        raise VideoError("card_video needs at least one text line")
    total = len(text_files) * float(duration_per_line)
    chain = []
    for idx, text_file in enumerate(text_files):
        start = idx * float(duration_per_line)
        end = start + float(duration_per_line)
        chain.append(
            _drawtext(
                Path(text_file),
                font=font,
                fontsize=fontsize,
                x="(w-text_w)/2",
                y="(h-text_h)/2",
                enable=f"'between(t,{start:.3f},{end:.3f})'",
            )
        )
    return [
        str(ffmpeg), "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={background}:s={int(width)}x{int(height)}:d={total:.3f}:r={int(fps)}",
        "-vf", ",".join(chain),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(int(fps)), "-t", f"{total:.3f}",
        str(out),
    ]


async def card_video(
    lines: Sequence[str],
    out: str | Path,
    *,
    width: int = 640,
    height: int = 360,
    duration_per_line: float = 2.0,
    fps: int = DEFAULT_FPS,
    fontsize: int = DEFAULT_FONTSIZE,
    background: str = DEFAULT_BACKGROUND,
    text_dir: str | Path | None = None,
    timeout: float = 300.0,
    ffmpeg: str = "ffmpeg",
    gen_backend: str | None = None,
) -> Path:
    """Render ``lines`` as a drawtext slideshow; returns the output path."""
    _reject_gen_backend(gen_backend)
    texts = [wrap_for_width(str(line), width=width, fontsize=fontsize) for line in lines]
    if not texts:
        raise VideoError("card_video needs at least one line")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text_dir = Path(text_dir) if text_dir is not None else Path(tempfile.mkdtemp(prefix="hermes-omni-text-"))
    text_files = write_text_files(texts, text_dir, prefix="card", collapse_newlines=False)
    argv = card_video_argv(
        text_files, out,
        width=width, height=height, duration_per_line=duration_per_line, fps=fps,
        font=find_font(), fontsize=fontsize, background=background, ffmpeg=ffmpeg,
    )
    await run_ffmpeg(argv, timeout=timeout)
    return out


def _event_field(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _caption_events(events: Iterable[Any]) -> list[tuple[float, str]]:
    """``[(ts, text)]`` for caption events with text, sorted by ts."""
    out: list[tuple[float, str]] = []
    for event in events or ():
        kind = str(_event_field(event, "kind", "") or "").lower()
        text = _event_field(event, "text")
        if kind not in ("caption", "summary") or not text:
            continue
        try:
            ts = float(_event_field(event, "ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        out.append((ts, str(text)))
    out.sort(key=lambda item: item[0])
    return out


def annotate_video_argv(
    src: Path,
    events: Sequence[Any],
    text_files: Sequence[Path],
    out: Path,
    *,
    font: str | None = None,
    fontsize: int = 24,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """argv for the caption overlay (windows from consecutive event timestamps)."""
    captions = _caption_events(events)
    if not captions:
        raise VideoError("annotate_video needs at least one caption event with text")
    if len(text_files) != len(captions):
        raise VideoError(f"expected {len(captions)} text files, got {len(text_files)}")
    chain = []
    for idx, (ts, _text) in enumerate(captions):
        enable = f"'gte(t,{ts:.3f})'" if idx + 1 == len(captions) else f"'between(t,{ts:.3f},{captions[idx + 1][0]:.3f})'"
        chain.append(
            _drawtext(
                Path(text_files[idx]),
                font=font,
                fontsize=fontsize,
                x="(w-text_w)/2",
                y="h-th-28",
                enable=enable,
                box=True,
            )
        )
    return [
        str(ffmpeg), "-v", "error", "-y",
        "-i", str(src),
        "-vf", ",".join(chain),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
        str(out),
    ]


async def annotate_video(
    src: str | Path,
    events: Sequence[Any],
    out: str | Path,
    *,
    fontsize: int = 24,
    text_dir: str | Path | None = None,
    timeout: float = 300.0,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    gen_backend: str | None = None,
) -> Path:
    """Overlay ``events`` (caption timestamps) on ``src``; returns the output path.

    Caption text is wrapped to the *source* frame width (ffprobe) so long
    captions do not get cropped off both sides of narrow clips.
    """
    _reject_gen_backend(gen_backend)
    src = Path(src)
    if not src.exists():
        raise VideoError(f"annotate_video: source not found: {src}")
    captions = _caption_events(events)
    if not captions:
        raise VideoError("annotate_video needs at least one caption event with text")
    size = await probe_size(src, ffprobe=ffprobe)
    frame_width = size[0] if size else 640
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text_dir = Path(text_dir) if text_dir is not None else Path(tempfile.mkdtemp(prefix="hermes-omni-text-"))
    texts = [
        wrap_for_width(text, width=max(64, frame_width - 40), fontsize=fontsize)
        for _ts, text in captions
    ]
    text_files = write_text_files(texts, text_dir, prefix="ann", collapse_newlines=False)
    argv = annotate_video_argv(
        src, events, text_files, out, font=find_font(), fontsize=fontsize, ffmpeg=ffmpeg
    )
    await run_ffmpeg(argv, timeout=timeout)
    return out


def relay_frames_argv(
    width: int,
    height: int,
    out: Path,
    *,
    fps: float = 2.0,
    pix_fmt: str = "rgb24",
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """argv for piping raw frames on stdin into an MP4."""
    return [
        str(ffmpeg), "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", f"{int(width)}x{int(height)}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out),
    ]


async def relay_frames(
    frames: Iterable[Frame] | AsyncIterator[Frame],
    out: str | Path,
    *,
    fps: float = 2.0,
    timeout: float = 300.0,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Raw frame sequence → MP4 (all frames must share size + packed format)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    iterator = _iter_frames(frames)
    try:
        first = await iterator.__anext__()
    except StopAsyncIteration:
        raise VideoError("relay_frames needs at least one frame") from None
    fmt = first.format
    if first.channels is None or not isinstance(first.data, (bytes, bytearray, memoryview)):
        raise VideoError(f"relay_frames needs packed raw frames, got format={fmt!r}")
    argv = relay_frames_argv(first.width, first.height, out, fps=fps, pix_fmt=fmt, ffmpeg=ffmpeg)

    async def _chunks() -> AsyncIterator[bytes]:
        yield bytes(first.data)
        async for frame in iterator:
            if frame.width != first.width or frame.height != first.height or frame.format != fmt:
                raise VideoError(
                    f"frame {frame.width}x{frame.height}/{frame.format} does not match "
                    f"{first.width}x{first.height}/{fmt} — relay_frames needs uniform frames"
                )
            yield bytes(frame.data)

    await stream_to_ffmpeg(argv, _chunks(), timeout=timeout)
    return out


async def _iter_frames(frames: Iterable[Frame] | AsyncIterator[Frame]) -> AsyncIterator[Frame]:
    if hasattr(frames, "__aiter__"):
        async for frame in frames:  # type: ignore[union-attr]
            yield frame
    else:
        for frame in frames:  # type: ignore[union-attr]
            yield frame


# ── subprocess plumbing (module-level so tests can patch one seam) ───────────


async def run_ffmpeg(
    argv: Sequence[str], *, stdin: bytes | None = None, timeout: float = 300.0, nice: bool = True
) -> CommandResult:
    """Run an ffmpeg render to completion; raises :class:`VideoError` on failure."""
    argv = [str(a) for a in argv]
    full = [*RUN_NICE, *argv] if nice else list(argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise VideoError(f"failed to spawn {full[0]!r}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise VideoError(f"ffmpeg timed out after {timeout}s: {' '.join(argv)}") from None
    result = CommandResult(
        argv=full,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=(out or b"").decode("utf-8", "replace"),
        stderr=(err or b"").decode("utf-8", "replace"),
    )
    if not result.ok:
        raise VideoError(f"ffmpeg rc={result.returncode}: {result.stderr.strip()[-300:]}")
    return result


async def stream_to_ffmpeg(
    argv: Sequence[str],
    chunks: AsyncIterator[bytes],
    *,
    timeout: float = 300.0,
) -> CommandResult:
    """Spawn ffmpeg and feed it ``chunks`` on stdin (used by relay_frames)."""
    argv = [str(a) for a in argv]
    full = [*RUN_NICE, *argv]
    try:
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise VideoError(f"failed to spawn {full[0]!r}: {exc}") from exc

    async def _read(stream: Any) -> bytes:
        return await stream.read() if stream is not None else b""

    async def _feed() -> None:
        assert proc.stdin is not None
        try:
            async for chunk in chunks:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, RuntimeError):
                pass

    feeder = asyncio.ensure_future(_feed())
    try:
        await asyncio.wait_for(feeder, timeout=timeout)
        stdout, stderr = await asyncio.gather(_read(proc.stdout), _read(proc.stderr))
        returncode = await proc.wait()
    except Exception as exc:
        feeder.cancel()
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        if isinstance(exc, asyncio.TimeoutError):
            raise VideoError(f"ffmpeg timed out after {timeout}s: {' '.join(argv)}") from None
        if isinstance(exc, VideoError):
            raise
        raise VideoError(f"ffmpeg feed failed: {exc}") from exc
    result = CommandResult(
        argv=full,
        returncode=returncode if returncode is not None else -1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )
    if not result.ok:
        raise VideoError(f"ffmpeg rc={result.returncode}: {result.stderr.strip()[-300:]}")
    return result


def _reject_gen_backend(gen_backend: str | None) -> None:
    if gen_backend:
        raise VideoError(
            f"generative video backend {gen_backend!r} is not implemented (seam only). "
            "Implement card_video/annotate_video/relay_frames for the new backend instead; "
            "see render.py module docstring."
        )