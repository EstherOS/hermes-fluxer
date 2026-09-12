"""Sense/part vocabulary for the fluxer omni engine (spec §6, wave 4).

The engine is built around one idea: **nothing downstream cares whether one
model or five serve the senses**.  Everything that is not the engine itself
(the adapter, the voice bridge, the video duplexer, tests) talks in terms of

* :class:`Sense` — text / image / audio / video,
* :class:`Direction` — ``IN`` (understanding) / ``OUT`` (expression),
* :class:`Part` — one piece of content flowing through the seams,
* :class:`SenseBinding` — which backend serves one sense-direction slot,
* :class:`DuplexSession` — the single-god-model seam (open / send / receive /
  close) that both a stitched cascade and a future unified model implement.

Slots are named ``"<sense>_<direction>"`` (``audio_in``, ``video_out`` …);
:data:`SLOTS` is the canonical set and config validation rejects typos against
it (did-you-mean included).

This module is import-light on purpose (stdlib only) — backends that need
subprocesses or HTTP import what they need when they are *built*, not when the
engine types are imported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence

__all__ = [
    "OmniError",
    "BackendError",
    "BackendNotConfigured",
    "HostRequired",
    "ProfileError",
    "Sense",
    "Direction",
    "SLOTS",
    "Part",
    "SenseBinding",
    "DuplexSession",
    "SenseBackend",
    "DuplexBackend",
    "slot_name",
    "is_slot",
    "sense_of_kind",
    "UNIFIED_CONFIG_EXAMPLE",
]


# ── errors ───────────────────────────────────────────────────────────────────


class OmniError(Exception):
    """Base class for every omni-engine failure."""


class BackendError(OmniError):
    """A backend ran but failed (subprocess rc != 0, timeout, HTTP error…)."""


class BackendNotConfigured(OmniError):
    """A slot has no usable backend (unbound slot, placeholder, unknown name)."""


class HostRequired(OmniError):
    """The ``agent`` marker: this step is serviced by the adapter core, not here."""


class ProfileError(OmniError):
    """A profile config is invalid; ``errors`` carries every validation problem."""

    def __init__(self, errors: Sequence[str], *, profile: str | None = None) -> None:
        self.errors = [str(e) for e in errors]
        where = f"profile '{profile}': " if profile else ""
        super().__init__(where + "; ".join(self.errors))


# ── senses ───────────────────────────────────────────────────────────────────


class Sense(str, Enum):
    """The four senses the engine reason about."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class Direction(str, Enum):
    """``IN`` = understanding, ``OUT`` = expression."""

    IN = "in"
    OUT = "out"


def _sense_value(sense: Sense | str) -> str:
    if isinstance(sense, Sense):
        return sense.value
    value = str(sense).strip().lower()
    if value in {s.value for s in Sense}:
        return value
    raise ValueError(f"unknown sense {sense!r}; expected one of {[s.value for s in Sense]}")


def _direction_value(direction: Direction | str) -> str:
    if isinstance(direction, Direction):
        return direction.value
    value = str(direction).strip().lower()
    if value in {d.value for d in Direction}:
        return value
    raise ValueError(f"unknown direction {direction!r}; expected 'in' or 'out'")


#: Canonical slot names, in a stable order (config validation order, docs order).
SLOTS: tuple[str, ...] = (
    "text_in",
    "text_out",
    "talker",
    "thinker",
    "image_in",
    "audio_in",
    "audio_out",
    "video_in",
    "video_out",
)


def slot_name(sense: Sense | str, direction: Direction | str) -> str:
    """``(Sense.AUDIO, Direction.IN) -> "audio_in"``."""
    return f"{_sense_value(sense)}_{_direction_value(direction)}"


def is_slot(name: str) -> bool:
    return name in SLOTS


def sense_of_kind(kind: str) -> Sense:
    """Map a :class:`Part` ``kind`` onto a :class:`Sense` (raises ``ValueError``)."""
    return Sense(_sense_value(kind))


# ── parts ────────────────────────────────────────────────────────────────────


@dataclass
class Part:
    """One piece of content flowing through the engine.

    ``kind`` is the sense as a short string (``"text"``/``"image"``/``"audio"``/
    ``"video"``).  ``data`` is deliberately loosely typed: text for ``text``,
    raw bytes for small media, or a filesystem path (``str``/:class:`Path`) for
    anything a backend should read itself.  ``meta`` carries per-part extras
    (timestamps, backend names, prompts, …); it is always a plain dict.
    """

    kind: str
    data: Any = None
    mime: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = str(self.kind).strip().lower()
        if self.meta is None:
            self.meta = {}
        elif not isinstance(self.meta, dict):
            self.meta = dict(self.meta)

    # convenience constructors -------------------------------------------------
    @classmethod
    def text(cls, data: str, **meta: Any) -> "Part":
        return cls("text", str(data), "text/plain", meta)

    @classmethod
    def audio(cls, data: Any, mime: str | None = "audio/wav", **meta: Any) -> "Part":
        return cls("audio", data, mime, meta)

    @classmethod
    def image(cls, data: Any, mime: str | None = "image/jpeg", **meta: Any) -> "Part":
        return cls("image", data, mime, meta)

    @classmethod
    def video(cls, data: Any, mime: str | None = "video/mp4", **meta: Any) -> "Part":
        return cls("video", data, mime, meta)

    # helpers -------------------------------------------------------------------
    @property
    def sense(self) -> Sense:
        return sense_of_kind(self.kind)

    @property
    def is_text(self) -> bool:
        return self.kind == "text"

    def text_of(self) -> str:
        """``data`` as text; raises :class:`BackendError` for non-text parts."""
        if not self.is_text:
            raise BackendError(f"expected a text part, got kind={self.kind!r}")
        return str(self.data)


# ── bindings ─────────────────────────────────────────────────────────────────


@dataclass
class SenseBinding:
    """Which backend serves a slot, plus the options and fallback backends used to build it.

    ``fallback`` is an optional ordered list of alternative backend names.
    When the primary backend fails with :class:`BackendError`, the cascade
    tries each fallback in sequence (see :class:`~hermes_omni.session.FallbackChain`).
    """

    backend: str
    options: dict[str, Any] = field(default_factory=dict)
    fallback: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.backend = str(self.backend).strip()
        if not self.backend:
            raise ProfileError(["binding has an empty backend name"])
        self.options = dict(self.options or {})
        self.fallback = [str(f).strip() for f in self.fallback]
        if not all(self.fallback):
            raise ProfileError(["fallback list contains an empty backend name"])

    @classmethod
    def from_config(cls, value: Any) -> "SenseBinding":
        """Accept ``"local.piper"`` or ``{"backend": "local.piper", "options": {...}}``."""
        if isinstance(value, str):
            return cls(value, {})
        if isinstance(value, Mapping):
            backend = value.get("backend") or value.get("name")
            if not backend:
                raise ProfileError(["binding mapping needs a 'backend' key"])
            options = value.get("options") or {}
            if not isinstance(options, Mapping):
                raise ProfileError(["binding 'options' must be a mapping"])
            fallback = value.get("fallback") or []
            if not isinstance(fallback, (list, tuple, set)):
                raise ProfileError(["binding 'fallback' must be a list of backend names"])
            fallback = [str(f).strip() for f in fallback]
            if not all(fallback):
                raise ProfileError(["binding 'fallback' contains an empty name"])
            unknown = set(value) - {"backend", "name", "options", "fallback"}
            if unknown:
                raise ProfileError([f"unknown binding key(s): {sorted(unknown)}"])
            return cls(str(backend), dict(options), fallback)
        raise ProfileError([f"binding must be a string or mapping, got {type(value).__name__}"])


# ── protocols (the seams) ────────────────────────────────────────────────────


class DuplexSession(Protocol):
    """The single-god-model seam: one session serving the senses.

    Implemented by the future unified backend's session and by
    :class:`fluxer.omni.cascade.CascadeSession` for stitched profiles, so
    callers cannot tell the two apart.
    """

    async def open(self) -> None:
        """Start the session (idempotent)."""

    async def send(self, part: Part) -> None:
        """Push one input part; outputs arrive via :meth:`receive`."""

    def receive(self) -> AsyncIterator[Part]:
        """Async iterator over output parts (ends after :meth:`close`)."""

    async def close(self) -> None:
        """Tear down, ending any active :meth:`receive` iterator."""


class SenseBackend(Protocol):
    """Turn-based transformer serving one slot (e.g. audio_in, audio_out)."""

    name: str

    async def process(self, part: Part) -> Sequence[Part]:
        ...


class DuplexBackend(Protocol):
    """Session-oriented backend for a single unified model."""

    name: str

    async def open_session(self, **options: Any) -> DuplexSession:
        ...


# ── role-specific protocol interfaces (spec-roles-profiles.md §2) ────────


class Cancellable(Protocol):
    """Backend that can be interrupted mid-turn."""

    async def cancel(self) -> None:
        """Stop the current operation as quickly as possible.

        Must free resources and reset so the backend is ready for a new
        call.  If the backend cannot honour cancellation (blocking subprocess
        without pipe), raise :class:`UncancelableError`.
        """
        ...


class UncancelableError(OmniError):
    """A backend cannot be cancelled — ignore its output and proceed."""


class AudioIn(Cancellable, Protocol):
    """Speech → text (ears)."""

    async def transcribe(self, wav_bytes: bytes, *, sample_rate: int = 16000) -> str:
        """Complete WAV → transcript."""

    def transcribe_stream(self, chunk_stream: AsyncIterator[bytes]) -> AsyncIterator[str]:
        """Streaming PCM → incremental text (optional; default yields single transcript)."""


class AudioOut(Cancellable, Protocol):
    """Text → speech (mouth)."""

    async def synthesize(self, text: str, *, voice: str | None = None) -> bytes:
        """Text → complete WAV bytes."""

    def synthesize_stream(
        self, text_stream: AsyncIterator[str], *, voice: str | None = None
    ) -> AsyncIterator[bytes]:
        """Streaming text → streaming audio chunks (optional)."""


class Vision(Cancellable, Protocol):
    """Image/video frames → text description (eyes)."""

    async def describe(self, image_bytes: bytes, *, prompt: str | None = None) -> str:
        """Single image → description."""

    def describe_stream(
        self, frames: AsyncIterator[bytes], *, fps: float = 0.5
    ) -> AsyncIterator[str]:
        """Frame stream → description stream (for live screen/webcam)."""


class Text(Cancellable, Protocol):
    """Conversational text generation (talker / thinker)."""

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        brief: str | None = None,
    ) -> AsyncIterator[str]:
        """Standard message list → token stream.  ``brief`` injects a role-specific
        system preface.  Yields text chunks as they are generated."""


class Realtime(Cancellable, Protocol):
    """Duplex speech session (audio in + audio out)."""

    async def start_session(
        self,
        *,
        mic: AsyncIterator[bytes],
        speaker: AsyncIterator[bytes] | None = None,
    ) -> "RealtimeSession":
        """Open a bidirectional stream session.

        ``mic`` = incoming PCM chunks (from VAD/microphone).
        ``speaker`` = echo-cancellation reference (optional).
        Returns a :class:`RealtimeSession` for reading model output.
        """

    @property
    def can_interrupt(self) -> bool:
        """Whether this backend supports barge-in natively."""
        ...


class RealtimeSession(Protocol):
    """Bidirectional stream wrapper for a :class:`Realtime` backend."""

    async def feed_input(self, chunk: bytes) -> None:
        """Push audio bytes into the model (raw PCM @ 16 kHz)."""

    def read_output(self) -> AsyncIterator[bytes]:
        """Yield audio chunks (raw PCM @ 24 kHz or codec-dependent) as
        they are generated by the model."""


class OmniBackend(Protocol):
    """Future god-model backend (all senses, one session).  Reserved."""

    async def connect(self, endpoint: str, *, config: dict) -> DuplexSession:
        ...

    async def disconnect(self) -> None:
        ...


#: Config shape for a *future* unified model.  Nothing consumes it yet except
#: documentation and the ``null_duplex`` error message — but when a model that
#: fits 6 GB appears, wiring it should be a config edit, not a code change.
UNIFIED_CONFIG_EXAMPLE: dict[str, Any] = {
    "omni": {
        "profiles": {
            "unified-future": {
                "mode": "unified",
                "backend": "openai_realtime",  # e.g. a future local omni endpoint
                "senses": ["text", "audio", "image", "video"],
                "options": {
                    "base_url": "http://127.0.0.1:8090/v1",
                    "model": "qwen2.5-omni-3b",
                    "modalities": ["text", "audio"],
                    "transport": "websocket",  # or "http"
                },
            }
        }
    }
}
