"""Test that omnimaker can be imported, key types instantiated, and the registry
works with a minimal v2 profile parse.  Prints a summary of all exported names.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package is on sys.path (workspace-relative)
_pkg = Path(__file__).resolve().parent.parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

import omnimaker
import pytest


def test_import_succeeds() -> None:
    """The top-level package import works without error."""
    assert omnimaker is not None


def test_part_instantiation() -> None:
    """Part dataclass can be created with each convenience constructor."""
    t = omnimaker.Part.text("hello")
    assert t.kind == "text"
    assert t.data == "hello"
    assert t.mime == "text/plain"

    a = omnimaker.Part.audio(b"RIFF")
    assert a.kind == "audio"
    assert a.mime == "audio/wav"

    i = omnimaker.Part.image(b"\xff\xd8\xff")
    assert i.kind == "image"
    assert i.mime == "image/jpeg"

    v = omnimaker.Part.video(b"\x00")
    assert v.kind == "video"
    assert v.mime == "video/mp4"


def test_sense_binding_instantiation() -> None:
    """SenseBinding parses string shorthand and full mapping."""
    sb = omnimaker.SenseBinding("local.piper")
    assert sb.backend == "local.piper"
    assert sb.options == {}

    sb2 = omnimaker.SenseBinding.from_config({
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

    spec = omnimaker.register_backend(
        "test.dummy", Dummy, kind="sense", description="test",
    )
    try:
        assert spec.name == "test.dummy"
        assert spec.kind == "sense"

        built = omnimaker.get_backend("test.dummy", answer=42)
        assert isinstance(built, Dummy)
        assert built.opts == {"answer": 42}

        names = omnimaker.list_backends()
        assert "test.dummy" in names
        assert "local.piper" in names  # builtin
    finally:
        omnimaker.unregister_backend("test.dummy")
    assert "test.dummy" not in omnimaker.list_backends()


def test_minimal_v2_profile_parse() -> None:
    """A minimal v2 component-graph profile can be parsed."""
    from omnimaker.engine.graph import parse_component_config

    rp = parse_component_config(
        "test",
        {
            "components": {
                "ears": {"ins": {"audio": ["user"]}},
            },
        },
    )
    assert rp.name == "test"
    assert isinstance(rp, omnimaker.ComponentGraphProfile)
    assert "ears" in rp.graph.component_names()


def test_print_summary(capsys) -> None:
    """Print all exported names for documentation / debugging."""
    names = sorted(
        n for n in dir(omnimaker) if not n.startswith("_")
    )
    print("=== omnimaker exported names ===")
    for n in names:
        obj = getattr(omnimaker, n)
        print(f"  {n}: {type(obj).__name__}")
    captured = capsys.readouterr()
    assert "Part" in captured.out
    assert "ComponentGraphProfile" in captured.out


def test_parse_component_config_exported() -> None:
    """parse_component_config is importable from omnimaker."""
    from omnimaker import parse_component_config

    assert callable(parse_component_config)


def test_component_graph_profile_exported() -> None:
    """ComponentGraphProfile is importable from omnimaker."""
    from omnimaker import ComponentGraphProfile

    assert ComponentGraphProfile is not None


def test_session_cancellable_mixin_importable() -> None:
    """CancellableMixin is still importable from omnimaker.session."""
    from omnimaker.session import CancellableMixin

    assert CancellableMixin is not None