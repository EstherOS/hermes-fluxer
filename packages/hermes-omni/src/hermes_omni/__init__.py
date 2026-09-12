"""
hermes-omni — Realtime/omni engine for Hermes Agent (v2 / component graph only).

Role-tagged multimodal backends, component-graph profiles,
and transport-agnostic realtime audio/video pipeline.

Usage:
    from hermes_omni import ComponentGraphProfile, Session
    profile = parse_component_config(name, spec)
    session = Session(profile)
    await session.start()
"""

from .backends.registry import (
    backend_catalog,
    get_backend,
    list_backends,
    register_backend,
    register_builtin_backends,
    unregister_backend,
)
from .engine.graph import (
    ComponentGraph,
    ComponentGraphProfile,
    PushRoute,
    parse_component_config,
)
from .profiles.cascade import Cascade, CascadeSession, duplex_session
from .session import (
    CancellableMixin,
    FallbackChain,
    PreBufferRing,
    Session,
)
from .session.thinker_bridge import BridgeResult, ThinkerBridge
from .types import (
    SLOTS,
    AudioIn,
    AudioOut,
    BackendError,
    BackendNotConfigured,
    Cancellable,
    Direction,
    DuplexBackend,
    DuplexSession,
    HostRequired,
    OmniError,
    Part,
    ProfileError,
    Realtime,
    Sense,
    SenseBackend,
    SenseBinding,
    Text,
    UncancelableError,
    Vision,
    is_slot,
    slot_name,
)

__all__ = [
    "SLOTS",
    "AudioIn",
    "AudioOut",
    "BackendError",
    "BackendNotConfigured",
    "BridgeResult",
    "Cancellable",
    "CancellableMixin",
    "Cascade",
    "CascadeSession",
    "ComponentGraph",
    "ComponentGraphProfile",
    "Direction",
    "DuplexBackend",
    "DuplexSession",
    "FallbackChain",
    "HostRequired",
    "OmniError",
    "Part",
    "PreBufferRing",
    "ProfileError",
    "PushRoute",
    "Realtime",
    "Sense",
    "SenseBackend",
    "SenseBinding",
    "Session",
    "Text",
    "ThinkerBridge",
    "UncancelableError",
    "Vision",
    "backend_catalog",
    "duplex_session",
    "get_backend",
    "is_slot",
    "list_backends",
    "parse_component_config",
    "register_backend",
    "register_builtin_backends",
    "slot_name",
    "unregister_backend",
]