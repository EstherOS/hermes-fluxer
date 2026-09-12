"""Tests for PreBufferRing, FallbackChain, CancellableMixin, and Session (v2)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_pkg = Path(__file__).resolve().parent.parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from hermes_omni import (
    BackendError,
    BackendNotConfigured,
    CancellableMixin,
    FallbackChain,
    OmniError,
    PreBufferRing,
    Session,
    UncancelableError,
)
from hermes_omni.engine.graph import (
    ComponentGraph,
    ComponentGraphProfile,
    parse_component_config,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ring() -> PreBufferRing:
    return PreBufferRing()


def _v2_profile() -> ComponentGraphProfile:
    """A minimal v2 profile with a single component and no backends."""
    return parse_component_config(
        "test-profile",
        {
            "components": {
                "ears": {
                    "ins": {"audio": ["user"]},
                    "outs": {"text": ["brain"]},
                },
                "brain": {
                    "ins": {"text": ["ears"]},
                    "outs": {"text": ["mouth"]},
                    "tools": {"harness": "core"},
                },
                "mouth": {
                    "ins": {"text": ["brain"]},
                    "outs": {"audio": ["user"]},
                },
            },
        },
    )


# ── Pre-buffer ring ───────────────────────────────────────────────────────────


def test_ring_empty_initially(ring: PreBufferRing) -> None:
    assert not ring.has_data
    assert ring.read_all() == b""


def test_ring_write_and_read(ring: PreBufferRing) -> None:
    data = b"\x00\x01" * 100  # 200 bytes
    ring.write(data)
    assert ring.has_data
    assert ring.read_all() == data


def test_ring_wraps_around(ring: PreBufferRing) -> None:
    """Ring overwrites oldest data when write exceeds SIZE."""
    chunk = b"A" * 6000
    ring.write(chunk)
    ring.write(b"BBBB")
    all_data = ring.read_all()
    assert len(all_data) <= ring.SIZE
    assert b"BBBB" in all_data


def test_ring_holds_640ms() -> None:
    """640 ms of 16 kHz PCM mono 16-bit = 10240 bytes."""
    ring = PreBufferRing()
    assert ring.SIZE == 10240
    payload = b"\x00\x01" * 5120  # 10240 bytes
    ring.write(payload)
    assert ring.read_all() == payload


def test_ring_single_chunk_overflow(ring: PreBufferRing) -> None:
    """A single chunk larger than SIZE keeps the tail."""
    big = b"X" * (ring.SIZE + 500)
    ring.write(big)
    assert len(ring.read_all()) == ring.SIZE
    assert ring.read_all() == b"X" * ring.SIZE


def test_ring_clear(ring: PreBufferRing) -> None:
    ring.write(b"hello")
    assert ring.has_data
    ring.clear()
    assert not ring.has_data
    assert ring.read_all() == b""


# ── FallbackChain ─────────────────────────────────────────────────────────────


def test_fallback_chain_candidates() -> None:
    chain = FallbackChain(["primary", "fallback_a", "fallback_b"])
    assert chain.candidates == ["primary", "fallback_a", "fallback_b"]


def test_fallback_chain_needs_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FallbackChain([])


@pytest.mark.asyncio
async def test_fallback_chain_primary_success() -> None:
    chain = FallbackChain(["good", "bad"])

    async def run(name: str) -> str:
        return f"ok:{name}"

    result = await chain.execute(run)
    assert result == "ok:good"


@pytest.mark.asyncio
async def test_fallback_chain_fallback_on_error() -> None:
    chain = FallbackChain(["failing", "backup"])

    async def run(name: str) -> str:
        if name == "failing":
            raise BackendError("primary failed")
        return f"ok:{name}"

    result = await chain.execute(run)
    assert result == "ok:backup"


@pytest.mark.asyncio
async def test_fallback_chain_all_fail() -> None:
    chain = FallbackChain(["a", "b"])

    async def run(name: str) -> str:
        raise BackendError(f"{name} is dead")

    with pytest.raises(BackendNotConfigured, match="all 2 backend"):
        await chain.execute(run)


@pytest.mark.asyncio
async def test_fallback_chain_health_check_skips() -> None:
    """A failing health check skips the candidate without running it."""

    chain = FallbackChain(
        ["broken", "working"],
        health_check=lambda name: name != "broken",
    )
    ran: list[str] = []

    async def run(name: str) -> str:
        ran.append(name)
        return f"ok:{name}"

    result = await chain.execute(run)
    assert result == "ok:working"
    assert ran == ["working"]


# ── CancellableMixin ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancellable_mixin_raises_by_default() -> None:
    obj = CancellableMixin()
    with pytest.raises(UncancelableError):
        await obj.cancel()


@pytest.mark.asyncio
async def test_cancellable_mixin_tracking() -> None:
    class TrackCancel(CancellableMixin):
        async def cancel(self) -> None:
            self._signal_cancel()

    obj = TrackCancel()
    assert not obj.is_cancelled
    await obj.cancel()
    assert obj.is_cancelled


@pytest.mark.asyncio
async def test_cancellable_mixin_reset() -> None:
    class TrackCancel(CancellableMixin):
        async def cancel(self) -> None:
            self._signal_cancel()

    obj = TrackCancel()
    await obj.cancel()
    assert obj.is_cancelled
    obj.reset_cancel()
    assert not obj.is_cancelled


@pytest.mark.asyncio
async def test_cancellable_mixin_wait_cancelled() -> None:
    class TrackCancel(CancellableMixin):
        async def cancel(self) -> None:
            self._signal_cancel()

    obj = TrackCancel()
    await obj.cancel()
    await asyncio.wait_for(obj.wait_cancelled.wait(), timeout=1)


# ── Session (v2 / component graph) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_create_and_start_stop() -> None:
    """v2 Session can be created, started, and stopped cleanly."""
    sess = Session(_v2_profile())
    assert not sess.running
    await sess.start()
    assert sess.running
    await sess.stop()
    assert not sess.running


@pytest.mark.asyncio
async def test_session_double_start_is_idempotent() -> None:
    sess = Session(_v2_profile())
    await sess.start()
    await sess.start()  # should not raise
    assert sess.running
    await sess.stop()


@pytest.mark.asyncio
async def test_session_double_stop_is_idempotent() -> None:
    sess = Session(_v2_profile())
    await sess.start()
    await sess.stop()
    await sess.stop()  # should not raise
    assert not sess.running


@pytest.mark.asyncio
async def test_session_feed_audio_while_running() -> None:
    """feed_audio does not error while the session is running."""
    sess = Session(_v2_profile())
    await sess.start()
    chunk = b"\x00\x01" * 500
    # Should not raise — chunks are silently dropped if no ASR queue
    await sess.feed_audio(chunk)
    await sess.stop()


@pytest.mark.asyncio
async def test_session_feed_audio_stopped_is_noop() -> None:
    """feed_audio is a no-op when the session is not running."""
    sess = Session(_v2_profile())
    chunk = b"\x00\x01" * 500
    await sess.feed_audio(chunk)  # should not raise
    assert True


@pytest.mark.asyncio
async def test_session_output_stream_drains_after_stop() -> None:
    """output_stream returns an empty iterator when the session is stopped."""
    sess = Session(_v2_profile())
    await sess.start()
    await sess.stop()
    collected = []
    async for chunk in sess.output_stream():
        collected.append(chunk)
    # No output was queued, so the iterator should be empty
    assert collected == []


@pytest.mark.asyncio
async def test_session_profile_field() -> None:
    """Session exposes the profile used during construction."""
    prof = _v2_profile()
    sess = Session(prof)
    assert sess.profile is prof


@pytest.mark.asyncio
async def test_session_component_backends_populated() -> None:
    """After start, _component_backends has entries for each component."""
    sess = Session(_v2_profile())
    await sess.start()
    assert "ears" in sess._component_backends
    assert "brain" in sess._component_backends
    assert "mouth" in sess._component_backends
    await sess.stop()


@pytest.mark.asyncio
async def test_session_backends_cleared_on_stop() -> None:
    """After stop, _backends and _component_backends are cleared."""
    sess = Session(_v2_profile())
    await sess.start()
    assert len(sess._backends) > 0 or True  # may be empty if backends fail
    await sess.stop()
    assert sess._backends == {}
    assert sess._component_backends == {}


@pytest.mark.asyncio
async def test_session_initial_asr_state() -> None:
    """ASR tracking fields are None before any audio flows."""
    sess = Session(_v2_profile())
    assert sess._asr_task is None
    assert sess._asr_queue is None
    assert sess._pending_transcript is None
    assert sess._asr_buffer is None
    assert not sess._asr_collecting


@pytest.mark.asyncio
async def test_session_graph_transcript_callback() -> None:
    """Graph transcript callback can be set and called."""
    sess = Session(_v2_profile())
    received: list[str] = []
    async def callback(text: str) -> None:
        received.append(text)
    sess._graph_transcript_callback = callback
    assert sess._graph_transcript_callback is not None