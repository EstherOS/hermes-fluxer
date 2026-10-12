"""Video duplexer seam: frames in, events out (spec §6 W4).

The point of this module is a **clear in/out seam** that does not care whether
the frames come from a decoded file (ffmpeg) or a LiveKit track, and does not
care whether the "understanding" is SmolVLM2, a future unified model, or a
fake in a unit test:

* :class:`Frame` — implementation-agnostic frame (raw bytes + width/height/
  format + timestamp);
* :class:`VideoEvent` — what comes out (caption / motion / summary / error);
* :class:`VideoDuplexer` — ``open() / push_frame() / pull() / close()``.

Import-light: stdlib only.  LiveKit is imported lazily inside the backend
that attaches a track (``fluxer.video.local_backend``), never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

__all__ = ["VideoError", "Frame", "VideoEvent", "VideoDuplexer", "EVENT_KINDS"]

#: Event kinds the local backends currently emit.
EVENT_KINDS = ("caption", "motion", "summary", "error", "frame")

#: Packed pixel formats for which ``len(data) == width*height*channels``.
_PACKED = {"rgb24": 3, "bgr24": 3, "rgba": 4, "bgra": 4, "gray": 1, "l8": 1}


class VideoError(Exception):
    """Anything the video lane could not do (bad frame, ffmpeg rc, missing dep)."""


@dataclass
class Frame:
    """One frame, implementation-agnostic.

    ``format`` is a lower-case tag: ``"rgb24"``/``"rgba"``/``"bgr24"``/
    ``"gray"`` for raw packed data (validated against ``data`` length) or
    ``"jpeg"``/``"png"`` for compressed bytes / a file path in ``data``.
    ``ts`` is seconds since stream start (or since the epoch, if that is what
    the caller has — the seam only requires consistency).
    """

    data: Any
    width: int
    height: int
    format: str = "rgb24"
    ts: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.format = str(self.format).strip().lower()
        self.width = int(self.width)
        self.height = int(self.height)
        self.ts = float(self.ts)
        if self.width <= 0 or self.height <= 0:
            raise VideoError(f"frame must have positive dimensions, got {self.width}x{self.height}")
        channels = _PACKED.get(self.format)
        if channels is not None and not isinstance(self.data, (str, bytes, bytearray, memoryview)):
            raise VideoError(f"packed frame format {self.format!r} needs bytes-like data")
        if channels is not None and isinstance(self.data, (bytes, bytearray, memoryview)):
            expected = self.width * self.height * channels
            if len(self.data) != expected:
                raise VideoError(
                    f"frame data is {len(self.data)} bytes; {self.format} "
                    f"{self.width}x{self.height} needs {expected} (w*h*{channels})"
                )
        if not isinstance(self.meta, dict):
            self.meta = dict(self.meta or {})

    @property
    def channels(self) -> int | None:
        return _PACKED.get(self.format)

    @property
    def nbytes(self) -> int:
        try:
            return len(self.data)
        except TypeError:
            return 0


@dataclass
class VideoEvent:
    """What a duplexer emits: caption / motion / summary / error, with timestamp."""

    ts: float
    kind: str
    text: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ts = float(self.ts)
        self.kind = str(self.kind).strip().lower()
        if not isinstance(self.meta, dict):
            self.meta = dict(self.meta or {})

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "text": self.text, "meta": self.meta}


class VideoDuplexer(Protocol):
    """The seam: frames in, events out.  Implemented by ``LocalVideoDuplexer``."""

    async def open(self) -> None:
        """Start the duplexer (idempotent)."""

    async def push_frame(self, frame: Frame, ts: float | None = None) -> None:
        """Push one input frame (IN path)."""

    def pull(self) -> AsyncIterator[VideoEvent]:
        """Async iterator over output events; ends after :meth:`close` (OUT path)."""

    async def close(self) -> None:
        """Tear down, ending any active ``pull()`` iterator."""
