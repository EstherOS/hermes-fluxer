"""Fluxer omni integration — binding, backends, and session factory."""

from __future__ import annotations

from omnimaker.adapters.registry import registry
from omnimaker.compiler import compile_profile
from omnimaker.runtime import Session

from .backends import register_fluxer_backends


def create_omni_session(profile_yaml: dict, profile_name: str) -> Session:
    """Build and return an omni Session for the given profile.

    Must be called after the fluxer plugin is loaded (backends
    registered). Returns a Session ready for ``await start()``.
    """
    profile = compile_profile(profile_name, profile_yaml)
    return Session(profile)


__all__ = [
    "create_omni_session",
    "register_fluxer_backends",
]