"""Tests for the ThinkerBridge — async delegation, background thinking, media passthrough."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import pytest

_pkg = Path(__file__).resolve().parent.parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from omnimaker import (
    BackendError,
    BackendNotConfigured,
    BridgeResult,
    OmniError,
    Part,
    ThinkerBridge,
)
from omnimaker.profiles import ResolvedProfile, parse_profile
from omnimaker.types import Text


# ── mock backends ────────────────────────────────────────────────────────────


class MockChatBackend:
    """Simulates a Text-protocol chat backend (the thinker's text_out)."""

    name = "mock.chat"

    def __init__(self, *, tokens: list[str] | None = None, delay: float = 0) -> None:
        self._tokens = tokens or ["hello ", "world"]
        self._delay = delay
        self._calls: list[dict] = []

    async def chat(
        self,
        messages: Sequence[dict],
        *,
        brief: str | None = None,
    ) -> AsyncIterator[str]:
        self._calls.append({"messages": list(messages), "brief": brief})
        for token in self._tokens:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield token

    @property
    def calls(self) -> list[dict]:
        return list(self._calls)


class MockProcessBackend:
    """Simulates a SenseBackend with process()."""

    name = "mock.process"

    def __init__(self, *, reply: str | None = None) -> None:
        self._reply = reply or "mock reply"
        self._calls: list[Part] = []

    async def process(self, part: Part) -> list[Part]:
        self._calls.append(part)
        return [Part.text(self._reply)]

    @property
    def calls(self) -> list[Part]:
        return list(self._calls)


class MockAudioIn:
    """Simulates an AudioIn protocol backend (talker-side ASR)."""

    name = "mock.audio_in"

    def __init__(self, *, transcript: str = "mock transcript") -> None:
        self._transcript = transcript
        self._calls: list[bytes] = []

    async def transcribe(
        self, wav_bytes: bytes, *, sample_rate: int = 16000
    ) -> str:
        self._calls.append(wav_bytes)
        return self._transcript

    @property
    def calls(self) -> list[bytes]:
        return list(self._calls)


class MockVision:
    """Simulates a Vision protocol backend (talker-side describe)."""

    name = "mock.vision"

    def __init__(self, *, description: str = "mock description") -> None:
        self._description = description
        self._calls: list[bytes] = []

    async def describe(
        self, image_bytes: bytes, *, prompt: str | None = None
    ) -> str:
        self._calls.append(image_bytes)
        return self._description

    @property
    def calls(self) -> list[bytes]:
        return list(self._calls)


# ── helpers ─────────────────────────────────────────────────────────────────


def _profile(*, bindings: dict | None = None, mode: str = "stitched") -> ResolvedProfile:
    """Build a minimal profile for testing."""
    spec = {"mode": mode}
    if bindings:
        spec["bindings"] = bindings
    else:
        spec["bindings"] = {"text_out": {"backend": "mock.chat"}}
    return parse_profile("test-{mode}", spec, backend_kinds=None)


def _get_backend(name: str, **options: Any) -> Any:
    """Minimal registry substitute — returns mock backends by name."""
    mapping = {
        "mock.chat": MockChatBackend,
        "mock.process": MockProcessBackend,
        "mock.audio_in": MockAudioIn,
        "mock.vision": MockVision,
    }
    cls = mapping.get(name)
    if cls is None:
        raise BackendNotConfigured(f"unknown mock backend {name!r}")
    return cls(**options)


class _MockTalkerSession:
    """Minimal talker session stub for bridge tests."""

    def __init__(
        self,
        audio_in: Any = None,
        eyes: Any = None,
    ) -> None:
        self._audio_in = audio_in
        self._eyes = eyes


# ── construction / resolution ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_chat_backend() -> None:
    """Bridge resolves a chat-capable text_out backend."""
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, _get_backend
    )
    assert not bridge._resolved
    await bridge._resolve()
    assert bridge._resolved
    assert bridge.thinker_backend is not None
    assert hasattr(bridge.thinker_backend, "chat")


@pytest.mark.asyncio
async def test_resolve_unified() -> None:
    """Bridge resolves a unified-mode profile."""
    profile = parse_profile(
        "test-unified",
        {"mode": "unified", "backend": "mock.chat", "senses": ["text"]},
        backend_kinds=None,
    )
    bridge = ThinkerBridge(_MockTalkerSession(), profile, _get_backend)
    await bridge._resolve()
    assert bridge.thinker_backend is not None


@pytest.mark.asyncio
async def test_resolve_unified_no_backend() -> None:
    """Unified profile without a backend raises ProfileError (parse-time)."""
    with pytest.raises(OmniError, match="backend"):
        parse_profile(
            "test-bad",
            {"mode": "unified", "senses": ["text"]},
            backend_kinds=None,
        )


@pytest.mark.asyncio
async def test_no_thinker_backend_delegate_raises() -> None:
    """delegate raises when no text_out backend was resolved."""
    profile = parse_profile(
        "test-empty",
        {"mode": "stitched", "bindings": {"audio_in": {"backend": "mock.audio_in"}}},
        backend_kinds=None,
    )
    bridge = ThinkerBridge(_MockTalkerSession(), profile, _get_backend)
    with pytest.raises(BackendNotConfigured, match="text_out"):
        async for _ in bridge.delegate("hello", []):
            pass


# ── delegate ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegate_text_only() -> None:
    """delegate streams tokens from the thinker's chat backend."""
    backend = MockChatBackend(tokens=["one ", "two ", "three"])
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: backend
    )
    collected: list[str] = []
    async for token in bridge.delegate("hello", []):
        collected.append(token)
    assert "".join(collected) == "one two three"


@pytest.mark.asyncio
async def test_delegate_with_media_text_part() -> None:
    """Text media parts are prepended to the query."""
    backend = MockChatBackend(tokens=["ok"])
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: backend
    )
    collected: list[str] = []
    async for _token in bridge.delegate("hello", [Part.text("extra context")]):
        collected.append(_token)
    assert len(backend.calls) == 1
    messages = backend.calls[0]["messages"]
    assert "extra context" in messages[0]["content"]


@pytest.mark.asyncio
async def test_delegate_with_audio_media_talker_asr() -> None:
    """Audio media is transcribed via talker's audio_in fallback."""
    audio_in = MockAudioIn(transcript="user said hello")
    talker = _MockTalkerSession(audio_in=audio_in)
    backend = MockChatBackend(tokens=["ack"])
    profile = _profile()
    bridge = ThinkerBridge(talker, profile, lambda name, **kw: backend)
    collected: list[str] = []
    async for token in bridge.delegate("process this", [Part.audio(b"\x00" * 100)]):
        collected.append(token)
    assert "".join(collected) == "ack"
    assert len(audio_in.calls) == 1
    content = backend.calls[0]["messages"][0]["content"]
    assert "user said hello" in content


@pytest.mark.asyncio
async def test_delegate_with_audio_media_thinker_passthrough() -> None:
    """When thinker has audio_in backend, raw passthrough is used."""
    thinker_audio = MockAudioIn(transcript="thinker heard audio")
    talker = _MockTalkerSession(audio_in=MockAudioIn(transcript="talker transcript"))
    backend = MockChatBackend(tokens=["ok"])
    profile = _profile()
    profile.bindings["audio_in"] = __import__("omnimaker").SenseBinding.from_config("mock.audio_in")
    bridge = ThinkerBridge(talker, profile, lambda name, **kw: {
        "mock.chat": backend,
        "mock.audio_in": thinker_audio,
    }[name])
    async for _ in bridge.delegate("hello", [Part.audio(b"\x00" * 100)]):
        pass
    content = backend.calls[0]["messages"][0]["content"]
    assert "thinker heard audio" in content
    assert len(talker._audio_in.calls) == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delegate_with_image_media_talker_vision() -> None:
    """Image media is described via talker's vision fallback."""
    eyes = MockVision(description="a cat")
    talker = _MockTalkerSession(eyes=eyes)
    backend = MockChatBackend(tokens=["nice"])
    profile = _profile()
    bridge = ThinkerBridge(talker, profile, lambda name, **kw: backend)
    collected: list[str] = []
    async for token in bridge.delegate("what is this", [Part.image(b"\xff" * 100)]):
        collected.append(token)
    assert "".join(collected) == "nice"
    assert len(eyes.calls) == 1
    content = backend.calls[0]["messages"][0]["content"]
    assert "a cat" in content


@pytest.mark.asyncio
async def test_delegate_with_image_media_thinker_passthrough() -> None:
    """When thinker has image_in backend, raw passthrough is used."""
    thinker_vision = MockVision(description="thinker saw a dog")
    talker = _MockTalkerSession(eyes=MockVision(description="talker saw a cat"))
    backend = MockChatBackend(tokens=["ok"])
    profile = _profile()
    profile.bindings["image_in"] = __import__("omnimaker").SenseBinding.from_config("mock.vision")
    bridge = ThinkerBridge(talker, profile, lambda name, **kw: {
        "mock.chat": backend,
        "mock.vision": thinker_vision,
    }[name])
    async for _ in bridge.delegate("hello", [Part.image(b"\xff" * 100)]):
        pass
    content = backend.calls[0]["messages"][0]["content"]
    assert "thinker saw a dog" in content
    assert len(talker._eyes.calls) == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_delegate_fallback_to_process() -> None:
    """When thinker has process() but not chat(), process() fallback works."""
    thinker = MockProcessBackend(reply="fallback reply")
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: thinker
    )
    collected: list[str] = []
    async for token in bridge.delegate("hi", []):
        collected.append(token)
    assert "".join(collected) == "fallback reply"


# ── background thinking ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_think_in_background_completes() -> None:
    """Background task completes and stores the result."""
    backend = MockChatBackend(tokens=["background ", "result"])
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: backend
    )
    task = await bridge.think_in_background("deep thought", [])
    await asyncio.wait_for(task, timeout=5)
    assert bridge.is_background_done
    assert bridge.background_result == "background result"


@pytest.mark.asyncio
async def test_check_background_waits_for_result() -> None:
    """check_background waits for the background task."""
    backend = MockChatBackend(tokens=["slow ", "reply"], delay=0.05)
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: backend
    )
    await bridge.think_in_background("slow query", [])
    result = await bridge.check_background(timeout=2)
    assert result == "slow reply"


@pytest.mark.asyncio
async def test_check_background_no_task() -> None:
    """check_background returns None when no background task was started."""
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: MockChatBackend()
    )
    result = await bridge.check_background()
    assert result is None


@pytest.mark.asyncio
async def test_background_task_property() -> None:
    """background_task property returns the active task."""
    backend = MockChatBackend(tokens=["x"])
    profile = _profile()
    bridge = ThinkerBridge(
        _MockTalkerSession(), profile, lambda name, **kw: backend
    )
    assert bridge.background_task is None
    task = await bridge.think_in_background("test", [])
    assert bridge.background_task is task


# ── media processing failures ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audio_media_no_backend() -> None:
    """Audio without talker ASR or thinker passthrough yields no text."""
    talker = _MockTalkerSession()  # no audio_in
    backend = MockChatBackend(tokens=["ok"])
    profile = _profile()
    bridge = ThinkerBridge(talker, profile, lambda name, **kw: backend)
    async for _ in bridge.delegate("hello", [Part.audio(b"\x00" * 100)]):
        pass
    content = backend.calls[0]["messages"][0]["content"]
    assert "[Audio transcript:" not in content


@pytest.mark.asyncio
async def test_image_media_no_backend() -> None:
    """Image without talker vision or thinker passthrough yields no text."""
    talker = _MockTalkerSession()  # no eyes
    backend = MockChatBackend(tokens=["ok"])
    profile = _profile()
    bridge = ThinkerBridge(talker, profile, lambda name, **kw: backend)
    async for _ in bridge.delegate("hello", [Part.image(b"\xff" * 100)]):
        pass
    content = backend.calls[0]["messages"][0]["content"]
    assert "[Image description:" not in content


# ── BridgeResult ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_result_success() -> None:
    """BridgeResult stores success and wakes waiters."""
    r = BridgeResult()
    assert r.text is None
    r.set_result("done")
    assert r.text == "done"
    assert await r.wait() == "done"


@pytest.mark.asyncio
async def test_bridge_result_error() -> None:
    """BridgeResult stores error and raises on wait."""
    r = BridgeResult()
    r.set_error(ValueError("boom"))
    assert r.error is not None
    with pytest.raises(ValueError, match="boom"):
        await r.wait()


@pytest.mark.asyncio
async def test_bridge_result_timeout() -> None:
    """BridgeResult.wait returns None on timeout."""
    r = BridgeResult()
    result = await r.wait(timeout=0.01)
    assert result is None


# ── run with: python -m pytest tests/test_thinker_bridge.py -v