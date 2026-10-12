"""Tests for Cascade.push(), CascadeSession, output_senses filtering, and error propagation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_pkg = Path(__file__).resolve().parent.parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from omnimaker import (
    BackendError,
    BackendNotConfigured,
    OmniError,
    Part,
)
from omnimaker.profiles import ResolvedProfile
from omnimaker.profiles.cascade import Cascade, CascadeSession, _coerce_parts, duplex_session

# ── Fake backends ──────────────────────────────────────────────────────────────


class FakeASR:
    name = "fake.asr"

    async def process(self, part: Part) -> list[Part]:
        return [Part.text("transcribed text", backend=self.name, audio=str(part.data))]


class FakeTTS:
    name = "fake.tts"

    def __init__(self):
        self.seen: list[str] = []

    async def process(self, part: Part) -> list[Part]:
        self.seen.append(part.text_of())
        return [Part.audio(b"WAV:" + part.text_of().encode(), backend=self.name)]


def _catalog(fakes: dict):
    def get(name: str, **opts):
        return fakes[name]

    return get


def _stitched(*slots: str) -> ResolvedProfile:
    """Build a minimal stitched ResolvedProfile for the given slot names."""
    from omnimaker.profiles import parse_profile

    bindings = {slot: {"backend": f"fake.{slot.replace('_', '')}"} for slot in slots}
    return parse_profile("fake", {"mode": "stitched", "bindings": bindings}, backend_kinds=None)


# ── Cascade.push() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_push_audio_understand_only() -> None:
    """Audio input goes through ASR and yields the transcript."""
    asr = FakeASR()
    prof = _stitched("audio_in")
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": asr}))
    out = [p async for p in c.push(Part.audio("input.wav"))]
    assert [(p.kind, p.data) for p in out] == [("text", "transcribed text")]


@pytest.mark.asyncio
async def test_cascade_push_text_passthrough() -> None:
    """Text input is passed through unchanged (no understand step)."""
    prof = _stitched("audio_out")
    tts = FakeTTS()
    c = Cascade(prof, get_backend=_catalog({"fake.audioout": tts}))
    out = [p async for p in c.push(Part.text("direct text"), output_senses=("audio",))]
    assert out[0].data == b"WAV:direct text"


@pytest.mark.asyncio
async def test_cascade_push_with_think() -> None:
    """The think hook transforms the payload and shows up in the output."""
    asr, tts = FakeASR(), FakeTTS()

    async def think(text: str, parts: list[Part]) -> str:
        assert text == "transcribed text"
        return "agent reply"

    prof = _stitched("audio_in", "audio_out")
    c = Cascade(
        prof,
        get_backend=_catalog({"fake.audioin": asr, "fake.audioout": tts}),
        think=think,
    )
    out = [p async for p in c.push(Part.audio("input.wav"), output_senses=("audio",))]
    assert [(p.kind, p.data) for p in out] == [
        ("text", "transcribed text"),
        ("text", "agent reply"),
        ("audio", b"WAV:agent reply"),
    ]
    assert tts.seen == ["agent reply"]


@pytest.mark.asyncio
async def test_cascade_push_string_think() -> None:
    """A sync think returning a string is normalized to Part.text."""
    tts = FakeTTS()
    prof = _stitched("audio_out")
    c = Cascade(
        prof,
        get_backend=_catalog({"fake.audioout": tts}),
        think=lambda text, parts: "reply text",
    )
    out = [p async for p in c.push(Part.text("user"), output_senses=("audio",))]
    assert [p.data for p in out] == ["reply text", b"WAV:reply text"]


# ── output_senses filtering ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_senses_filter_text_only() -> None:
    """With 'text' as the only output sense, no express step runs."""
    prof = _stitched("audio_in")
    asr = FakeASR()
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": asr}), output_senses=())
    out = [p async for p in c.push(Part.audio("x.wav"), output_senses=("text",))]
    assert [p.data for p in out] == ["transcribed text"]


@pytest.mark.asyncio
async def test_output_senses_filter_no_audio_when_not_requested() -> None:
    """TTS is not called when 'audio' is not in output_senses."""
    tts = FakeTTS()
    prof = _stitched("audio_out")
    c = Cascade(prof, get_backend=_catalog({"fake.audioout": tts}), output_senses=())
    out = [p async for p in c.push(Part.text("hi"), output_senses=())]
    assert out == []  # no output requested
    assert tts.seen == []  # TTS never called


@pytest.mark.asyncio
async def test_output_senses_instance_level() -> None:
    """Instance-level output_senses are used when per-call is None."""
    tts = FakeTTS()
    prof = _stitched("audio_out")
    c = Cascade(
        prof,
        get_backend=_catalog({"fake.audioout": tts}),
        output_senses=("audio",),
    )
    out = [p async for p in c.push(Part.text("hi"))]
    assert out, "should have audio output"
    assert tts.seen == ["hi"]


# ── Error propagation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_missing_input_binding() -> None:
    """Missing input slot raises BackendNotConfigured."""
    prof = _stitched("audio_out")  # has audio_out but no audio_in
    c = Cascade(prof, get_backend=_catalog({}))
    with pytest.raises(BackendNotConfigured, match="audio_in"):
        await c.collect(Part.audio("x.wav"))


@pytest.mark.asyncio
async def test_cascade_missing_output_binding() -> None:
    """Missing output slot raises BackendNotConfigured."""
    prof = _stitched("audio_in")
    asr = FakeASR()
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": asr}))
    with pytest.raises(BackendNotConfigured, match="audio_out"):
        await c.collect(Part.audio("x.wav"), output_senses=("audio",))


@pytest.mark.asyncio
async def test_cascade_backend_process_error() -> None:
    """A broken backend raises BackendError through the cascade."""

    class BrokenBackend:
        name = "broken"

        async def process(self, part: Part) -> list[Part]:
            raise BackendError("internal error")

    prof = _stitched("audio_in")
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": BrokenBackend()}))
    with pytest.raises(BackendError, match="internal error"):
        await c.collect(Part.audio("x.wav"))


@pytest.mark.asyncio
async def test_cascade_rejects_unified_profile() -> None:
    """Cascade constructor raises OmniError for a unified profile."""
    from omnimaker.profiles import parse_profile

    prof = parse_profile(
        "u",
        {"mode": "unified", "backend": "null_duplex", "senses": ["audio"]},
        backend_kinds=None,
    )
    with pytest.raises(OmniError, match="stitched-only"):
        Cascade(prof)


# ── CascadeSession (DuplexSession) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cascade_session_fifo() -> None:
    """CascadeSession buffers outputs from send() and yields them in order."""
    asr = FakeASR()
    prof = _stitched("audio_in")
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": asr}))

    session = c.session()
    await session.open()
    received: list[Part] = []

    async def drain():
        async for part in session.receive():
            received.append(part)

    task = asyncio.ensure_future(drain())
    await session.send(Part.audio("one.wav"))
    await session.close()
    await asyncio.wait_for(task, timeout=5)

    assert [p.data for p in received] == ["transcribed text"]
    assert session.sent == 1
    assert session.emitted == 1


@pytest.mark.asyncio
async def test_cascade_session_rejects_send_after_close() -> None:
    prof = _stitched("audio_in")
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": FakeASR()}))
    session = c.session()
    await session.open()
    await session.close()
    with pytest.raises(OmniError, match="closed"):
        await session.send(Part.text("x"))


@pytest.mark.asyncio
async def test_cascade_session_open_twice_safe() -> None:
    prof = _stitched("audio_in")
    c = Cascade(prof, get_backend=_catalog({"fake.audioin": FakeASR()}))
    session = c.session()
    await session.open()
    await session.open()  # idempotent
    await session.close()


@pytest.mark.asyncio
async def test_duplex_session_helper_stitched() -> None:
    """duplex_session() returns a CascadeSession for stitched profiles."""
    prof = _stitched("audio_in")
    session = await duplex_session(prof, get_backend=_catalog({"fake.audioin": FakeASR()}))
    assert isinstance(session, CascadeSession)
    await session.close()


@pytest.mark.asyncio
async def test_duplex_session_helper_unified_raises() -> None:
    """duplex_session() with a unified placeholder raises BackendNotConfigured."""
    from omnimaker.profiles import parse_profile

    prof = parse_profile(
        "u",
        {"mode": "unified", "backend": "null_duplex", "senses": ["audio"]},
        backend_kinds=None,
    )
    with pytest.raises(BackendNotConfigured, match="not configured"):
        await duplex_session(prof)


# ── _coerce_parts ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coerce_parts_none() -> None:
    assert await _coerce_parts(None) == []


@pytest.mark.asyncio
async def test_coerce_parts_string() -> None:
    parts = await _coerce_parts("hello")
    assert len(parts) == 1
    assert parts[0].kind == "text" and parts[0].data == "hello"


@pytest.mark.asyncio
async def test_coerce_parts_part() -> None:
    p = Part.text("x")
    assert await _coerce_parts(p) == [p]


@pytest.mark.asyncio
async def test_coerce_parts_list() -> None:
    parts = await _coerce_parts([Part.text("a"), Part.text("b")])
    assert [p.data for p in parts] == ["a", "b"]


@pytest.mark.asyncio
async def test_coerce_parts_async_iter() -> None:
    async def gen():
        yield Part.text("a")
        yield Part.text("b")

    parts = await _coerce_parts(gen())
    assert [p.data for p in parts] == ["a", "b"]


@pytest.mark.asyncio
async def test_coerce_parts_awaitable() -> None:
    async def get_part() -> str:
        return "awaitable result"

    parts = await _coerce_parts(get_part())
    assert parts[0].data == "awaitable result"


@pytest.mark.asyncio
async def test_coerce_parts_unknown_raises() -> None:
    with pytest.raises(BackendError, match="cannot interpret"):
        await _coerce_parts(42)