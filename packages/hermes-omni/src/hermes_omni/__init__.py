"""
hermes-omni — Realtime/omni engine for Hermes Agent.

Role-tagged multimodal backends, profile composition, session FSM,
cancellation protocol, and transport-agnostic realtime audio/video pipeline.

Usage:
    from hermes_omni import Profile, Session
    profile = Profile.from_config(yaml_data)
    session = Session(profile)
    await session.start()
"""

from .types import (
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
    SLOTS,
    Text,
    UncancelableError,
    Vision,
    is_slot,
    slot_name,
)
from .backends.registry import (
    backend_catalog,
    get_backend,
    list_backends,
    register_backend,
    register_builtin_backends,
    unregister_backend,
)
from .profiles import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILE_SPEC,
    ResolvedProfile,
    parse_profile,
    resolve_profile,
)
from .profiles.cascade import Cascade, CascadeSession, duplex_session
from .session import (
    BridgeResult,
    CancellableMixin,
    FallbackChain,
    PreBufferRing,
    Session,
    SessionFSM,
    SessionState,
    ThinkerBridge,
)

__all__ = [
    "AudioIn",
    "AudioOut",
    "BackendError",
    "BackendNotConfigured",
    "BridgeResult",
    "Cancellable",
    "CancellableMixin",
    "Cascade",
    "CascadeSession",
    "DEFAULT_PROFILE_NAME",
    "DEFAULT_PROFILE_SPEC",
    "Direction",
    "DuplexBackend",
    "DuplexSession",
    "FallbackChain",
    "HostRequired",
    "OmniError",
    "Part",
    "PreBufferRing",
    "ProfileError",
    "Realtime",
    "ResolvedProfile",
    "Sense",
    "SenseBackend",
    "SenseBinding",
    "Session",
    "SessionFSM",
    "SessionState",
    "SLOTS",
    "Text",
    "ThinkerBridge",
    "UncancelableError",
    "Vision",
    "backend_catalog",
    "duplex_session",
    "get_backend",
    "is_slot",
    "list_backends",
    "parse_profile",
    "register_backend",
    "register_builtin_backends",
    "resolve_profile",
    "slot_name",
    "unregister_backend",
]