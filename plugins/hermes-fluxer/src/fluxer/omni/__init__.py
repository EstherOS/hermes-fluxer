"""Fluxer omni engine (spec §6, wave 4).

Strong seams between modes without forcing a separation: senses/parts and the
:class:`~fluxer.omni.types.DuplexSession` protocol are the vocabulary, the
registry holds thin backends, profiles say which backend serves which slot,
and :class:`~fluxer.omni.cascade.Cascade` turns stitched bindings into a
duplex-like facade so callers treat stitched and unified identically.

Nothing downstream cares whether one model or five serve the senses.
"""

from .types import (
    BackendError,
    BackendNotConfigured,
    Direction,
    DuplexBackend,
    DuplexSession,
    HostRequired,
    OmniError,
    Part,
    ProfileError,
    Sense,
    SenseBackend,
    SenseBinding,
    SLOTS,
    UNIFIED_CONFIG_EXAMPLE,
    is_slot,
    slot_name,
)
from .registry import (
    backend_catalog,
    describe_backends,
    get_backend,
    list_backends,
    register_backend,
    register_builtin_backends,
    unregister_backend,
)
from .profile import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILE_SPEC,
    ResolvedProfile,
    omni_section,
    parse_profile,
    resolve_profile,
)
from .cascade import Cascade, CascadeSession, duplex_session

__all__ = [
    # types
    "Sense",
    "Direction",
    "SLOTS",
    "Part",
    "SenseBinding",
    "DuplexSession",
    "SenseBackend",
    "DuplexBackend",
    "slot_name",
    "is_slot",
    "UNIFIED_CONFIG_EXAMPLE",
    # errors
    "OmniError",
    "BackendError",
    "BackendNotConfigured",
    "HostRequired",
    "ProfileError",
    # registry
    "register_backend",
    "unregister_backend",
    "get_backend",
    "list_backends",
    "describe_backends",
    "backend_catalog",
    "register_builtin_backends",
    # profiles
    "ResolvedProfile",
    "resolve_profile",
    "parse_profile",
    "omni_section",
    "DEFAULT_PROFILE_NAME",
    "DEFAULT_PROFILE_SPEC",
    # cascade
    "Cascade",
    "CascadeSession",
    "duplex_session",
]
