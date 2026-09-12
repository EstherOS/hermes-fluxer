"""Shared setup for the fluxer adapter unit tests.

Puts ``plugin-src`` on ``sys.path`` so the plugin package imports as ``fluxer``, and
registers the dynamic ``Platform("fluxer")`` enum member the same way the gateway's
platform registry does at plugin registration time (spec §2.4 / integration §1.3).
"""

import sys
from pathlib import Path

PLUGIN_SRC = Path(__file__).resolve().parents[2]
if str(PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SRC))

from gateway.config import Platform  # noqa: E402

if "fluxer" not in Platform._value2member_map_:
    Platform._add_pseudo_member("fluxer")
