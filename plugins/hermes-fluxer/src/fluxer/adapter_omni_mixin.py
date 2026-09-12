"""OmniAdapterMixin — hermes-omni integration for FluxerAdapter.

Extract the omni engine wiring into a reusable mixin so the adapter's
voice dispatch can use proper typed backends without bloating the main
adapter module.

Usage in FluxerAdapter:

    class FluxerAdapter(BasePlatformAdapter, OmniAdapterMixin):
        ...
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class OmniAdapterMixin:
    """Mixin that adds hermes-omni profile loading and STT/TTS dispatch.

    Expected instance attributes (set by FluxerAdapter.__init__):
        _omni_cfg: dict | None      — omni section from Hermes config
        _omni_profile: Any | None   — resolved profile
        _omni_session: Any | None   — hermes_omni.session.Session
    """

    # Set by the mixin at init time
    _omni_cfg: dict | None = None
    _omni_profile: Any | None = None
    _omni_session: Any | None = None

    def _init_omni_backends(self, omni_cfg: dict) -> None:
        """Wire hermes-omni from the ``omni:`` config section.

        Called once from the adapter's __init__ when the config has
        an omni section and hermes_omni is importable.  Resolves the
        default profile, validates every binding, creates a Session.
        """
        from hermes_omni import register_builtin_backends, register_backend
        from hermes_omni.profiles import detect_config_version as detect_v1
        from hermes_omni.engine.graph import detect_config_version as detect_v2, parse_component_config

        register_builtin_backends()
        # Register the torch Mini-Omni2 duplex backend for unified profiles
        # Register the torch Mini-Omni2 duplex backend for unified profiles
        try:
            from hermes_omni.backends.miniomni2 import MiniOmni2DuplexBackend
            register_backend("local.miniomni2_duplex", MiniOmni2DuplexBackend, kind="duplex")
            logger.info("Fluxer: registered torch Mini-Omni2 duplex backend")
        except Exception as exc:
            logger.debug("Fluxer: torch Mini-Omni2 backend not available (%s)", exc)

        # Register the CrispASR Vulkan-accelerated mini-omni2 backend
        try:
            from hermes_omni.backends.crispasr import CrispAsrMiniOmni2Backend
            register_backend("local.crispasr_miniomni2", CrispAsrMiniOmni2Backend, kind="duplex")
            logger.info("Fluxer: registered CrispASR mini-omni2 backend (Vulkan)")
        except Exception as exc:
            logger.debug("Fluxer: CrispASR backend not available (%s)", exc)

        # Register the CrispASR streaming backend (stdin/stdout pipe)
        try:
            from hermes_omni.backends.crispasr_stream import CrispAsrStreamingBackend
            register_backend("local.crispasr_stream", CrispAsrStreamingBackend, kind="duplex")
            logger.info("Fluxer: registered CrispASR streaming backend (stdin pipe)")
        except Exception as exc:
            logger.debug("Fluxer: CrispASR streaming backend not available (%s)", exc)

        profile_name = omni_cfg.get("default_profile", "split-local")
        profile_spec = omni_cfg.get("profiles", {}).get(profile_name, {})
        ver = detect_v2(profile_spec)
        if ver == "v2":
            from hermes_omni.engine.graph import parse_component_config
            self._omni_profile = parse_component_config(profile_name, profile_spec)
            logger.info(
                "Fluxer: omni v2 profile %r resolved (mode=%s, components=%s)",
                profile_name,
                self._omni_profile.graph.infer_profile_mode(),
                list(self._omni_profile.graph.component_names()),
            )
        else:
            from hermes_omni.profiles import resolve_profile
            self._omni_profile = resolve_profile(omni_cfg, name=profile_name)
            logger.info(
                "Fluxer: omni v1 profile %r resolved (mode=%s, slots=%s)",
                profile_name,
                self._omni_profile.mode,
                list(self._omni_profile.bindings.keys()),
            )
        from hermes_omni.session import Session

        self._omni_session = Session(self._omni_profile)
        logger.info("Fluxer: hermes-omni Session created")

    def _omni_synthesize(self, text: str) -> bytes | None:
        """Synthesize speech via hermes-omni; returns WAV bytes or None."""
        if self._omni_session is None:
            return None
        try:
            audio_out = self._omni_session._audio_out
            if audio_out is not None:
                from hermes_omni.backends.adapters import AudioOutFromSenseBackend

                adapter = AudioOutFromSenseBackend(audio_out)
                result = adapter.synthesize(text)
                if hasattr(result, "__await__"):
                    return asyncio.run(result)
                return result
        except Exception as exc:
            logger.warning("Fluxer: hermes-omni TTS failed (%s); legacy fallback", exc)
        return None

    def _omni_transcribe(self, wav_bytes: bytes) -> str | None:
        """Transcribe audio via hermes-omni; returns text or None."""
        if self._omni_session is None:
            return None
        try:
            audio_in = self._omni_session._audio_in
            if audio_in is not None:
                from hermes_omni.backends.adapters import AudioInFromSenseBackend

                adapter = AudioInFromSenseBackend(audio_in)
                result = adapter.transcribe(wav_bytes)
                if hasattr(result, "__await__"):
                    return asyncio.run(result)
                return result
        except Exception as exc:
            logger.warning("Fluxer: hermes-omni ASR failed (%s); legacy fallback", exc)
        return None
