"""Regression: lazy exports must resolve under FOREIGN package names.

The gateway loads the plugin as ``hermes_plugins.<slug>`` and the validate
probe as ``hermes_validate_probe_plugin``; the unit tests import it as
top-level ``fluxer`` (sys.path shim), which is why absolute ``fluxer.*``
lazy targets shipped unnoticed. This test loads the plugin under a foreign
name exactly like the validate probe does, then exercises the lazy surfaces.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_foreign(name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_lazy_voice_exports_resolve_under_foreign_name():
    name = "fluxer_loader_probe_pkg"
    try:
        _load_foreign(name)
        voice = importlib.import_module(f"{name}.voice")
        # Direct names
        assert voice.try_livekit.__name__ == "try_livekit"
        assert isinstance(voice.livekit_available(), bool)
        # PEP 562 lazy resolutions (would raise ModuleNotFoundError('fluxer') pre-fix)
        assert voice.VoiceConfig is not None
        assert callable(voice.parse_voice_config)
        assert voice.VoiceController is not None
        assert voice.VoiceCascade is not None
        assert voice.audio is not None
    finally:
        for k in [k for k in list(sys.modules) if k == name or k.startswith(name + ".")]:
            del sys.modules[k]


def test_module_meta_integrity_under_foreign_name():
    """The loaded module should carry the foreign name, not 'fluxer'."""
    name = "fluxer_loader_probe_pkg2"
    try:
        mod = _load_foreign(name)
        assert mod.__name__ == name
        voice = importlib.import_module(f"{name}.voice")
        assert voice.__name__ == f"{name}.voice"
    finally:
        for k in [k for k in list(sys.modules) if k == name or k.startswith(name + ".")]:
            del sys.modules[k]


def test_loads_text_only_when_omni_engine_missing():
    """A bare plugin copy (no ``omnimaker`` installed) must still load — text-only mode.

    Regression: ``adapter.py`` logged via the module ``logger`` before it was
    defined, so the no-engine fallback path raised ``NameError`` and plugin
    registration died instead of degrading gracefully.
    """
    name = "fluxer_noomni_probe_pkg"
    # Simulate a box without the omni engine: drop every cached omnimaker
    # module — an already-imported submodule would otherwise satisfy the
    # fallback import even with the top-level package halted.
    saved = {k: v for k, v in sys.modules.items() if k == "omnimaker" or k.startswith("omnimaker.")}
    for k in list(saved):
        del sys.modules[k]
    sys.modules["omnimaker"] = None  # any new omnimaker import -> ImportError
    try:
        mod = _load_foreign(name)
        adapter = importlib.import_module(f"{name}.adapter")
        assert adapter._omni_imported is False
        assert "omnimaker" in (adapter._omni_import_error or "").lower()
        assert callable(getattr(mod, "register", None))
    finally:
        sys.modules.pop("omnimaker", None)
        sys.modules.update(saved)
        for k in [k for k in list(sys.modules) if k == name or k.startswith(name + ".")]:
            del sys.modules[k]
