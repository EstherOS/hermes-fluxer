"""Test that hermes_omni can be imported, key types instantiated, and the registry
works with a minimal profile parse.  Prints a summary of all exported names."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package is on sys.path (workspace-relative)
_pkg = Path(__file__).resolve().parent.parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

import hermes_omni
import pytest


def test_import_succeeds() -> None:
    """The top-level package import works without error."""
    assert hermes_omni is not None


def test_part_instantiation() -> None:
    """Part dataclass can be created with each convenience constructor."""
    t = hermes_omni.Part.text("hello")
    assert t.kind == "text"
    assert t.data == "hello"
    assert t.mime == "text/plain"

    a = hermes_omni.Part.audio(b"RIFF")
    assert a.kind == "audio"
    assert a.mime == "audio/wav"

    i = hermes_omni.Part.image(b"\xff\xd8\xff")
    assert i.kind == "image"
    assert i.mime == "image/jpeg"

    v = hermes_omni.Part.video(b"\x00")
    assert v.kind == "video"
    assert v.mime == "video/mp4"


def test_sense_binding_instantiation() -> None:
    """SenseBinding parses string shorthand and full mapping."""
    sb = hermes_omni.SenseBinding("local.piper")
    assert sb.backend == "local.piper"
    assert sb.options == {}

    sb2 = hermes_omni.SenseBinding.from_config({
        "backend": "local.piper",
        "options": {"model": "/v.onnx"},
    })
    assert sb2.backend == "local.piper"
    assert sb2.options == {"model": "/v.onnx"}


def test_registry_register_and_retrieve() -> None:
    """Backends can be registered, retrieved, and unregistered."""
    class Dummy:
        name = "test.dummy"
        def __init__(self, **opts):
            self.opts = opts

    spec = hermes_omni.register_backend(
        "test.dummy", Dummy, kind="sense", description="test",
    )
    try:
        assert spec.name == "test.dummy"
        assert spec.kind == "sense"

        built = hermes_omni.get_backend("test.dummy", answer=42)
        assert isinstance(built, Dummy)
        assert built.opts == {"answer": 42}

        names = hermes_omni.list_backends()
        assert "test.dummy" in names
        assert "local.piper" in names  # builtin
    finally:
        hermes_omni.unregister_backend("test.dummy")
    assert "test.dummy" not in hermes_omni.list_backends()


def test_minimal_profile_parse() -> None:
    """A minimal stitched profile can be parsed without a catalog."""
    rp = hermes_omni.parse_profile(
        "test",
        {"mode": "stitched", "bindings": {"audio_in": "local.whispercpp"}},
        backend_kinds=None,
    )
    assert rp.name == "test"
    assert rp.mode == "stitched"
    assert rp.binding("audio_in").backend == "local.whispercpp"


def test_session_state_enum() -> None:
    """All expected SessionState values exist."""
    assert hermes_omni.SessionState.IDLE.name == "IDLE"
    assert hermes_omni.SessionState.LISTENING.name == "LISTENING"
    assert hermes_omni.SessionState.ANALYZING.name == "ANALYZING"
    assert hermes_omni.SessionState.THINKING.name == "THINKING"
    assert hermes_omni.SessionState.SPEAKING.name == "SPEAKING"
    assert hermes_omni.SessionState.PREEMPTING.name == "PREEMPTING"
    assert hermes_omni.SessionState.HALTED.name == "HALTED"
    assert len(hermes_omni.SessionState) == 7


def test_print_summary(capsys) -> None:
    """Print all exported names for documentation / debugging."""
    names = sorted(
        n for n in dir(hermes_omni) if not n.startswith("_")
    )
    print("=== hermes_omni exported names ===")
    for n in names:
        obj = getattr(hermes_omni, n)
        print(f"  {n}: {type(obj).__name__}")
    captured = capsys.readouterr()
    assert "Part" in captured.out
    assert "SessionState" in captured.out