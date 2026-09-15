"""pytest configuration for hermes-omni tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package source is on sys.path
_pkg = Path(__file__).resolve().parent / "src"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

# Enable asyncio mode for all async tests
pytest_plugins = ["pytest_asyncio"]