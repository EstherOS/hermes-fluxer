"""OmniAdapterMixin — hermes-omni integration for FluxerAdapter.

Loads a profile from the ``omni:`` config section, creates a session,
and registers backends. The bridge owns all audio I/O — the mixin is
just the init hook.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class OmniAdapterMixin:
    """Mixin adding hermes-omni backend registration + session creation.

    Expected instance attributes:
        _omni_cfg: dict | None
        _omni_session: Any | None
    """

    _omni_cfg: dict | None = None
    _omni_session: Any | None = None

    def _init_omni_backends(self, omni_cfg: dict) -> None:
        """Register backends and create the default session.

        Called once from the adapter's ``__init__`` when the config has
        an ``omni`` section.
        """
        from omnimaker import register_builtins, register_adapter
        from omnimaker.adapters.registry import registry

        # Engine builtins
        register_builtins()

        # Fluxer backends — STT and TTS
        from fluxer.omni.backends import (
            CrispAsrBackend, CrispAsrStreamBackend,
            PiperTTSBackend, KokoroTTSBackend,
        )
        register_adapter("local/crispasr", CrispAsrBackend)
        register_adapter("local/crispasr_stream", CrispAsrStreamBackend)
        register_adapter("local/piper", PiperTTSBackend)
        register_adapter("local/kokoro", KokoroTTSBackend)

        # LLM completion via OpenRouter
        try:
            from fluxer.omni.backends_llm import LLMCompletionAdapter
            register_adapter("local/llm-completion", LLMCompletionAdapter)
            logger.info("Fluxer: registered LLM completion backend")
        except Exception as exc:
            logger.debug("Fluxer: LLM completion backend not available (%s)", exc)

        # Local llama-server completion
        try:
            from fluxer.omni.backends_local import LlamaServerCompletion
            register_adapter("local/llama-completion", LlamaServerCompletion)
            logger.info("Fluxer: registered local llama-server completion")
        except Exception as exc:
            logger.debug("Fluxer: local llama-server not available (%s)", exc)

        # Hermes agent session adapter
        try:
            from omnimaker_hermes import HermesSessionBackend
            register_adapter("hermes/session", HermesSessionBackend)
            logger.info("Fluxer: registered hermes/session adapter")
        except Exception as exc:
            logger.debug("Fluxer: hermes/session not available (%s)", exc)

        # Fluxer binding
        from fluxer.omni.binding import FluxerBinding
        registry.register_binding("fluxer", FluxerBinding)
        logger.info("Fluxer: registered @fluxer binding")

        # Resolve and compile the default profile
        profile_name = omni_cfg.get("default_profile", "hal9000")
        profile_spec = omni_cfg.get("profiles", {}).get(profile_name, {})
        if not profile_spec:
            logger.warning("Fluxer: omni default profile %r not found", profile_name)
            return

        from omnimaker.compiler import compile_profile
        self._profile = compile_profile(profile_name, profile_spec)
        logger.info(
            "Fluxer: omni profile %r compiled (%d nodes, %d routes)",
            profile_name, len(self._profile.nodes), len(self._profile.routes),
        )

        from omnimaker.runtime import Session
        self._omni_session = Session(self._profile)
        logger.info("Fluxer: omni session created (%s)", self._omni_session.id)