"""Tests for Cascade._build() with FallbackChain integration.

Gap 2 acceptance criteria:
1. Cascade builds a backend from a slot with no fallback (existing behaviour preserved).
2. Cascade tries fallback on primary failure — falls through to the next candidate.
3. Cascade raises BackendNotConfigured when all fallbacks fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_pkg = Path(__file__).resolve().parent.parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from hermes_omni import (
    BackendError,
    BackendNotConfigured,
    Part,
    ProfileError,
    SenseBinding,
)
from hermes_omni.profiles.cascade import Cascade


# ── fakes ──────────────────────────────────────────────────────────────────────


class GoodBackend:
    """A backend that always succeeds."""
    name = "good"

    async def process(self, part: Part) -> list[Part]:
        return [Part.text("ok", backend=self.name)]


class FailingBackend:
    """A backend that always fails at build time."""
    name = "failing"

    def __init__(self) -> None:
        raise BackendError("build failure")


class SometimesFailingBackend:
    """A backend that fails for a configurable number of calls."""
    name = "sometimes"

    def __init__(self) -> None:
        self._built = False

    async def process(self, part: Part) -> list[Part]:
        return [Part.text("recovered", backend=self.name)]


# ── helpers ────────────────────────────────────────────────────────────────────


class _BackendBuilder:
    """A drop-in for ``get_backend`` that maps names to pre-built instances
    (or raises :class:`BackendNotConfigured` for unknown names)."""

    def __init__(self, backends: dict[str, object]) -> None:
        self._backends = backends

    def __call__(self, name: str, **options: object) -> object:
        if name not in self._backends:
            raise BackendNotConfigured(f"unknown backend {name!r}")
        return self._backends[name]


class _FactoryBuilder:
    """A ``get_backend`` that instantiates classes for each name."""

    def __init__(self, classes: dict[str, type]) -> None:
        self._classes = classes

    def __call__(self, name: str, **options: object) -> object:
        cls = self._classes.get(name)
        if cls is None:
            raise BackendNotConfigured(f"unknown backend {name!r}")
        return cls(**options)


def _profile(
    backend: str,
    *,
    fallback: list[str] | None = None,
) -> object:
    """Build a minimal :class:`ResolvedProfile` with one binding."""
    from hermes_omni import parse_profile

    binding: dict[str, object] = {"backend": backend}
    if fallback:
        binding["fallback"] = fallback

    spec = {
        "mode": "stitched",
        "bindings": {"audio_in": binding},
    }
    return parse_profile("test", spec, backend_kinds=None)


# ── no fallback (existing behaviour preserved) ────────────────────────────────


@pytest.mark.asyncio
async def test_build_no_fallback() -> None:
    """Cascade builds a backend from a slot with no fallback — unchanged path."""
    prof = _profile("good")
    c = Cascade(prof, get_backend=_BackendBuilder({"good": GoodBackend()}))
    backend = await c._build("audio_in")
    assert backend.name == "good"


@pytest.mark.asyncio
async def test_build_no_fallback_raises() -> None:
    """Missing backend raises BackendNotConfigured without fallback."""
    prof = _profile("missing")
    c = Cascade(prof, get_backend=_BackendBuilder({}))
    with pytest.raises(BackendNotConfigured, match="missing"):
        await c._build("audio_in")


@pytest.mark.asyncio
async def test_build_no_fallback_build_error() -> None:
    """A backend that errors at construction raises BackendError without fallback."""
    prof = _profile("failing")
    c = Cascade(prof, get_backend=_FactoryBuilder({"failing": FailingBackend}))
    with pytest.raises(BackendError, match="build failure"):
        await c._build("audio_in")


# ── fallback chain — primary failure → fallback succeeds ─────────────────────


@pytest.mark.asyncio
async def test_build_fallback_primary_missing() -> None:
    """When primary is unknown, fallback is tried and returned on success."""
    good = GoodBackend()
    prof = _profile("missing", fallback=["good"])
    c = Cascade(
        prof,
        get_backend=_BackendBuilder({"good": good}),
    )
    backend = await c._build("audio_in")
    assert backend is good


@pytest.mark.asyncio
async def test_build_fallback_primary_build_error() -> None:
    """When primary raises at build time, fallback is tried and returned."""
    prof = _profile("failing", fallback=["good"])
    c = Cascade(
        prof,
        get_backend=_FactoryBuilder({
            "failing": FailingBackend,
            "good": GoodBackend,
        }),
    )
    backend = await c._build("audio_in")
    assert isinstance(backend, GoodBackend)


@pytest.mark.asyncio
async def test_build_fallback_primary_then_fallback() -> None:
    """Primary fails, first fallback also fails, second fallback works."""
    prof = _profile("failing", fallback=["also_fails", "good"])
    c = Cascade(
        prof,
        get_backend=_FactoryBuilder({
            "failing": FailingBackend,
            "also_fails": FailingBackend,
            "good": GoodBackend,
        }),
    )
    backend = await c._build("audio_in")
    assert isinstance(backend, GoodBackend)


# ── fallback chain — all fail ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_fallback_all_missing() -> None:
    """When every fallback is unknown, BackendNotConfigured is raised."""
    prof = _profile("missing1", fallback=["missing2", "missing3"])
    c = Cascade(prof, get_backend=_BackendBuilder({}))
    with pytest.raises(BackendNotConfigured, match="all.*failed"):
        await c._build("audio_in")


@pytest.mark.asyncio
async def test_build_fallback_all_build_errors() -> None:
    """When every fallback raises, BackendNotConfigured is raised."""
    prof = _profile("failing", fallback=["also_fails"])
    c = Cascade(
        prof,
        get_backend=_FactoryBuilder({
            "failing": FailingBackend,
            "also_fails": FailingBackend,
        }),
    )
    with pytest.raises(BackendNotConfigured, match="all.*failed"):
        await c._build("audio_in")


# ── Fallback in the turn path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_with_fallback() -> None:
    """A full push() turn works with fallback — primary fails, fallback processes."""
    good_asr = GoodBackend()
    prof = _profile("missing", fallback=["good"])
    c = Cascade(
        prof,
        get_backend=_BackendBuilder({"good": good_asr}),
    )
    out = [p async for p in c.push(Part.audio(b"test.wav"))]
    assert len(out) == 1
    assert out[0].data == "ok"
    assert out[0].meta.get("backend") == "good"


@pytest.mark.asyncio
async def test_push_fallback_all_fail() -> None:
    """When all fallbacks fail, push() propagates BackendNotConfigured."""
    prof = _profile("missing1", fallback=["missing2"])
    c = Cascade(prof, get_backend=_BackendBuilder({}))
    with pytest.raises(BackendNotConfigured, match="all.*failed"):
        await c.collect(Part.audio(b"x.wav"))


# ── SenseBinding fallback parsing ──────────────────────────────────────────────


class TestSenseBindingFallback:

    def test_fallback_empty_by_default(self) -> None:
        b = SenseBinding("local.piper")
        assert b.fallback == []

    def test_fallback_from_string_shorthand(self) -> None:
        b = SenseBinding.from_config("local.piper")
        assert b.fallback == []

    def test_fallback_from_mapping(self) -> None:
        b = SenseBinding.from_config({
            "backend": "local.qwen3asr",
            "fallback": ["local.whispercpp"],
        })
        assert b.fallback == ["local.whispercpp"]
        assert b.backend == "local.qwen3asr"

    def test_fallback_multiple(self) -> None:
        b = SenseBinding.from_config({
            "backend": "primary",
            "fallback": ["fb1", "fb2", "fb3"],
        })
        assert b.fallback == ["fb1", "fb2", "fb3"]

    def test_fallback_rejects_non_list(self) -> None:
        with pytest.raises(ProfileError, match="must be a list"):
            SenseBinding.from_config({
                "backend": "x",
                "fallback": "not_a_list",
            })

    def test_fallback_rejects_empty_name(self) -> None:
        with pytest.raises(ProfileError, match="contains an empty name"):
            SenseBinding.from_config({
                "backend": "x",
                "fallback": ["good", ""],
            })

    def test_fallback_unknown_key_still_rejected(self) -> None:
        with pytest.raises(ProfileError, match="unknown binding key"):
            SenseBinding.from_config({
                "backend": "x",
                "fallback": ["y"],
                "unknown_key": "z",
            })

    def test_fallback_round_trip_through_profile(self) -> None:
        """Parsing a profile with fallback populates SenseBinding.fallback."""
        from hermes_omni import parse_profile

        spec = {
            "mode": "stitched",
            "bindings": {
                "audio_in": {
                    "backend": "local.qwen3asr",
                    "fallback": ["local.whispercpp"],
                },
            },
        }
        prof = parse_profile("test", spec, backend_kinds=None)
        binding = prof.binding("audio_in")
        assert binding is not None
        assert binding.backend == "local.qwen3asr"
        assert binding.fallback == ["local.whispercpp"]