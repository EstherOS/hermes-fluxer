"""Tests for the session FSM, pre-buffer ring, fallback chain, and cancellation."""

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
    SessionFSM,
    SessionState,
    UncancelableError,
)
from hermes_omni.profiles import ResolvedProfile

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fsm() -> SessionFSM:
    return SessionFSM()


@pytest.fixture
def ring() -> PreBufferRing:
    return PreBufferRing()


# ── FSM transitions ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fsm_initial_state(fsm: SessionFSM) -> None:
    assert fsm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_fsm_idle_to_listening(fsm: SessionFSM) -> None:
    await fsm.trigger_vad_open()
    assert fsm.state == SessionState.LISTENING


@pytest.mark.asyncio
async def test_fsm_full_cycle(fsm: SessionFSM) -> None:
    """IDLE -> LISTENING -> ANALYZING -> THINKING -> SPEAKING -> IDLE."""
    await fsm.trigger_vad_open()
    assert fsm.state == SessionState.LISTENING
    await fsm.trigger_vad_close()
    assert fsm.state == SessionState.ANALYZING
    await fsm.trigger_transcript("hello")
    assert fsm.state == SessionState.THINKING
    await fsm.trigger_first_token()
    assert fsm.state == SessionState.SPEAKING
    await fsm.trigger_speech_end()
    assert fsm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_fsm_preempting_interruption(fsm: SessionFSM) -> None:
    """SPEAKING -> PREEMPTING -> LISTENING (interruption mid-speech)."""
    await fsm.trigger_vad_open()
    await fsm.trigger_vad_close()
    await fsm.trigger_transcript("hello")
    await fsm.trigger_first_token()
    assert fsm.state == SessionState.SPEAKING

    # Simulate barge-in
    await fsm.trigger_interruption()
    # trigger_interruption fires on_cancel_all + awaits cancellation, then
    # transitions to LISTENING — so state should be LISTENING
    assert fsm.state == SessionState.LISTENING


@pytest.mark.asyncio
async def test_fsm_halted_is_terminal(fsm: SessionFSM) -> None:
    await fsm.trigger_fatal_error(RuntimeError("boom"))
    assert fsm.halted
    with pytest.raises(OmniError, match="halted"):
        await fsm.trigger_vad_open()


@pytest.mark.asyncio
async def test_fsm_invalid_transition_is_ignored(fsm: SessionFSM) -> None:
    """trigger_vad_close from IDLE is a no-op (not a valid transition)."""
    await fsm.trigger_vad_close()  # silently ignored
    assert fsm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_fsm_callbacks_wired() -> None:
    """Transition callbacks are invoked on state changes."""
    fsm2 = SessionFSM()
    called = []

    async def on_open() -> None:
        called.append("open")

    fsm2.on_vad_open = on_open
    await fsm2.trigger_vad_open()
    assert called == ["open"]


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


# ── Session (high-level integration) ──────────────────────────────────────────


def _minimal_profile() -> ResolvedProfile:
    """A minimal stitched profile for testing Session creation."""
    from hermes_omni import parse_profile

    return parse_profile(
        "test-profile",
        {
            "mode": "stitched",
            "bindings": {"text_out": {"backend": "agent"}},
        },
        backend_kinds=None,
    )


@pytest.mark.asyncio
async def test_session_vad_open_triggers_listening() -> None:
    sess = Session(_minimal_profile())
    await sess.start()
    assert sess.fsm.state == SessionState.IDLE
    await sess.fsm.trigger_vad_open()
    assert sess.fsm.state == SessionState.LISTENING


@pytest.mark.asyncio
async def test_session_vad_close_triggers_analyzing() -> None:
    sess = Session(_minimal_profile())
    await sess.start()
    await sess.fsm.trigger_vad_open()
    await sess.fsm.trigger_vad_close()
    assert sess.fsm.state == SessionState.ANALYZING


@pytest.mark.asyncio
async def test_session_interruption() -> None:
    """VAD open while SPEAKING triggers interruption (via FSM)."""
    sess = Session(_minimal_profile())
    await sess.start()
    await sess.fsm.trigger_vad_open()
    await sess.fsm.trigger_vad_close()
    await sess.fsm.trigger_transcript("hello")
    await sess.fsm.trigger_first_token()
    assert sess.fsm.state == SessionState.SPEAKING
    await sess.fsm.trigger_interruption()
    assert sess.fsm.state == SessionState.LISTENING


@pytest.mark.asyncio
async def test_session_ring_feed_audio() -> None:
    """feed_audio fills the pre-buffer ring via the FSM."""
    sess = Session(_minimal_profile())
    await sess.start()
    chunk = b"\x00\x01" * 500
    await sess.feed_audio(chunk)
    assert sess.fsm.prebuffer_bytes > 0


@pytest.mark.asyncio
async def test_session_output_finished() -> None:
    sess = Session(_minimal_profile())
    await sess.start()
    await sess.fsm.trigger_vad_close()
    await sess.fsm.trigger_transcript("hello")
    await sess.fsm.trigger_first_token()
    await sess.fsm.trigger_speech_end()
    assert sess.fsm.state == SessionState.IDLE


@pytest.mark.asyncio
async def test_session_fatal_error_halts() -> None:
    sess = Session(_minimal_profile())
    await sess.start()
    await sess.fsm.trigger_fatal_error(RuntimeError("test"))
    assert sess.fsm.halted