"""Profile parsing for the fluxer omni engine (spec §6, wave 4).

A profile is the config-level statement of "which backend serves which
sense".  Two modes:

``stitched``
    One binding per slot, e.g. ``audio_in: local.whispercpp`` +
    ``audio_out: local.piper`` — the verified cascade on this box
    (``docs/omni-models-feasibility.md`` §2/§5 phase 0).

``unified``
    One backend (a single god model) + the senses it serves, e.g.
    ``{mode: unified, backend: null_duplex, senses: [text, audio, image, video]}``.
    Nothing implements one on 6 GB yet — see
    :data:`hermes_omni.types.UNIFIED_CONFIG_EXAMPLE` for the shape a future
    model should be wired with.

Config shape (``cfg.extra`` on the adapter side)::

    omni:
      default_profile: local-stitched        # optional
      profiles:
        local-stitched:
          mode: stitched
          bindings:
            audio_in:  {backend: local.whispercpp}
            audio_out: {backend: local.piper}
            text_out:  {backend: agent}      # documents the thinker seam
        unified-future:
          mode: unified
          backend: null_duplex
          senses: [text, audio, image, video]

Bindings accept the ``"local.piper"`` string shorthand or the full mapping.
Validation is strict about typos (unknown slot keys get a did-you-mean);
unknown *spec* keys are warnings so unrelated voice config can ride along.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..types import (
    BackendNotConfigured,
    Part,
    ProfileError,
    Sense,
    SenseBinding,
    SLOTS,
    is_slot,
    slot_name,
)
from ..engine.graph import (
    ComponentGraph,
    ComponentGraphProfile,
    detect_config_version,
    parse_component_config,
)

__all__ = [
    "ResolvedProfile",
    "ComponentGraph",
    "ComponentGraphProfile",
    "resolve_profile",
    "parse_profile",
    "parse_component_config",
    "detect_config_version",
    "omni_section",
    "DEFAULT_PROFILE_NAME",
    "DEFAULT_PROFILE_SPEC",
]

#: Name of the builtin fallback profile used when no ``omni`` config exists.
DEFAULT_PROFILE_NAME = "local-stitched"

#: The builtin fallback: exactly the backends verified on this box (doc §2).
DEFAULT_PROFILE_SPEC: dict[str, Any] = {
    "mode": "stitched",
    "bindings": {
        "text_out": {"backend": "agent"},
        "image_in": {"backend": "local.smolvlm"},
        "audio_in": {"backend": "local.whispercpp"},
        "audio_out": {"backend": "local.piper"},
        "video_in": {"backend": "local.smolvlm_video"},
        "video_out": {"backend": "local.render"},
    },
}

_KNOWN_SPEC_KEYS = {"mode", "bindings", "backend", "senses", "options"}


@dataclass
class ResolvedProfile:
    """A parsed, validated profile — what the engine actually executes.

    ``bindings`` maps slot → :class:`SenseBinding` (stitched mode; empty for
    unified).  ``backend`` is the unified backend binding (unified mode only).
    ``senses`` lists the senses a unified backend serves.  ``warnings`` holds
    non-fatal notes (unknown spec keys, …); ``source`` is ``"config"`` or
    ``"builtin"``.
    """

    name: str
    mode: str
    bindings: dict[str, SenseBinding] = field(default_factory=dict)
    backend: SenseBinding | None = None
    senses: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    source: str = "config"
    raw: dict[str, Any] = field(default_factory=dict)

    def binding(self, slot: str) -> SenseBinding | None:
        return self.bindings.get(slot)

    def binding_for(self, kind: str) -> SenseBinding | None:
        """Binding for the input slot of a part kind (``"audio"`` → ``audio_in``)."""
        return self.bindings.get(slot_name(kind, "in"))

    def require_binding(self, slot: str) -> SenseBinding:
        binding = self.bindings.get(slot)
        if binding is None:
            raise BackendNotConfigured(
                f"profile {self.name!r} has no binding for slot {slot!r}; "
                f"bound slots: {sorted(self.bindings)}"
            )
        return binding


def omni_section(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pull the ``omni`` section out of a config mapping (tolerates both shapes)."""
    if not cfg:
        return {}
    raw = cfg.get("omni")
    if isinstance(raw, Mapping):
        return dict(raw)
    if "profiles" in cfg or "default_profile" in cfg:  # cfg *is* the omni section
        return dict(cfg)
    return {}


def _did_you_mean(key: str, candidates: Sequence[str]) -> str:
    close = difflib.get_close_matches(str(key), list(candidates), n=1, cutoff=0.6)
    return f" (did you mean {close[0]!r}?)" if close else ""


def _normalize_senses(values: Any) -> tuple[str, ...]:
    """Accept ``["audio", "audio_in", "AUDIO"]`` → ``("audio", …)`` (deduped, ordered)."""
    if not isinstance(values, (list, tuple, set)):
        raise ProfileError([f"'senses' must be a list, got {type(values).__name__}"])
    valid = {s.value for s in Sense}
    out: list[str] = []
    for value in values:
        text = str(value).strip().lower()
        text = text[: -len("_in")] if text.endswith("_in") else text
        text = text[: -len("_out")] if text.endswith("_out") else text
        if text not in valid:
            raise ProfileError([f"unknown sense {value!r}; expected one of {sorted(valid)}"])
        if text not in out:
            out.append(text)
    if not out:
        raise ProfileError(["'senses' must list at least one sense"])
    return tuple(out)


def parse_profile(
    name: str,
    spec: Mapping[str, Any],
    *,
    backend_kinds: Mapping[str, str] | None = None,
) -> ResolvedProfile:
    """Parse one profile spec into a :class:`ResolvedProfile`.

    ``backend_kinds`` (``{name: kind}``, see :func:`hermes_omni.backends.registry.backend_catalog`)
    turns unknown-backend names into errors; kind mismatches (a duplex backend
    bound to a sense slot, or a sense backend as a unified backend) are always
    errors.
    """
    if not isinstance(spec, Mapping):
        raise ProfileError([f"profile spec must be a mapping, got {type(spec).__name__}"], profile=name)

    errors: list[str] = []
    warnings: list[str] = []
    unknown_keys = sorted(set(spec) - _KNOWN_SPEC_KEYS)
    if unknown_keys:
        warnings.append(f"ignoring unknown profile key(s): {unknown_keys}")

    mode = spec.get("mode")
    if mode is None:
        if "backend" in spec:
            mode = "unified"
        elif "bindings" in spec:
            mode = "stitched"
    mode = str(mode).strip().lower() if mode is not None else None
    if mode not in ("stitched", "unified"):
        raise ProfileError(
            [f"missing/unknown mode {spec.get('mode')!r}; expected 'stitched' or 'unified'"], profile=name
        )

    if mode == "stitched":
        raw_bindings = spec.get("bindings")
        if not isinstance(raw_bindings, Mapping) or not raw_bindings:
            raise ProfileError(["stitched profile needs a non-empty 'bindings' mapping"], profile=name)
        bindings: dict[str, SenseBinding] = {}
        for key, value in raw_bindings.items():
            key = str(key)
            if not is_slot(key):
                errors.append(f"unknown slot {key!r}{_did_you_mean(key, SLOTS)}")
                continue
            try:
                binding = SenseBinding.from_config(value)
            except ProfileError as exc:
                errors.extend(str(e) for e in exc.errors)
                continue
            if backend_kinds is not None:
                kind = backend_kinds.get(binding.backend)
                if kind is None:
                    errors.append(f"unknown backend {binding.backend!r} for slot {key!r}")
                    continue
                if kind == "duplex":
                    errors.append(f"backend {binding.backend!r} is a duplex backend; slot {key!r} needs a sense backend")
                    continue
            bindings[key] = binding
        if errors:
            raise ProfileError(errors, profile=name)
        if not bindings:
            raise ProfileError(["no valid bindings"], profile=name)
        return ResolvedProfile(
            name=name, mode="stitched", bindings=bindings, warnings=warnings,
            source="config", raw=dict(spec),
        )

    # unified
    raw_backend = spec.get("backend")
    if raw_backend is None:
        raise ProfileError(["unified profile needs a 'backend'"], profile=name)
    try:
        backend = SenseBinding.from_config(raw_backend)
    except ProfileError as exc:
        raise ProfileError(list(exc.errors), profile=name) from exc
    options = spec.get("options") or {}
    if not isinstance(options, Mapping):
        raise ProfileError(["'options' must be a mapping"], profile=name)
    backend.options = {**dict(options), **backend.options}
    if backend_kinds is not None:
        kind = backend_kinds.get(backend.backend)
        if kind is None:
            errors.append(f"unknown backend {backend.backend!r}")
        elif kind != "duplex":
            errors.append(f"backend {backend.backend!r} is a sense backend; unified mode needs a duplex backend")
    senses = _normalize_senses(spec.get("senses"))
    if errors:
        raise ProfileError(errors, profile=name)
    return ResolvedProfile(
        name=name, mode="unified", backend=backend, senses=senses, warnings=warnings,
        source="config", raw=dict(spec),
    )


def _builtin_fallback(backend_kinds: Mapping[str, str] | None = None) -> ResolvedProfile:
    profile = parse_profile(DEFAULT_PROFILE_NAME, DEFAULT_PROFILE_SPEC, backend_kinds=backend_kinds)
    profile.source = "builtin"
    return profile


def resolve_profile(
    cfg: Mapping[str, Any] | None,
    name: str | None = None,
    *,
    backend_kinds: Mapping[str, str] | None = None,
) -> ResolvedProfile:
    """Resolve ``name`` (or the config default) against ``cfg``.

    * config present + valid → that profile (``source="config"``);
    * no ``omni`` config at all → the builtin stitched fallback
      (``source="builtin"``, :data:`DEFAULT_PROFILE_NAME`);
    * a requested/defaulted name that is missing, or several profiles with no
      default, → :class:`ProfileError` listing what is available.

    When ``backend_kinds`` is None it is loaded lazily from the registry so
    typos in backend names are caught by default.
    """
    section = omni_section(cfg)
    profiles = section.get("profiles") or {}
    if not isinstance(profiles, Mapping):
        raise ProfileError(["'omni.profiless' must be a mapping"])
    # tolerate 'omni:' without 'profiles:' by treating the section itself as specs? no —
    # section-level keys are default_profile/profiles only; unknown ones warn below.
    if backend_kinds is None:
        from ..backends.registry import backend_catalog  # lazy: avoids a profile→registry import at load

        backend_kinds = backend_catalog()

    section_unknown = sorted(set(section) - {"profiles", "default_profile"})
    section_warnings = [f"ignoring unknown omni key(s): {section_unknown}"] if section_unknown else []

    if name is None:
        name = section.get("default_profile")
        if name is None:
            if DEFAULT_PROFILE_NAME in profiles:
                name = DEFAULT_PROFILE_NAME
            elif len(profiles) == 1:
                name = next(iter(profiles))

    if name is not None:
        name = str(name)
        if name not in profiles:
            if not profiles:
                if name == DEFAULT_PROFILE_NAME:
                    profile = _builtin_fallback(backend_kinds)
                    profile.warnings.extend(section_warnings)
                    return profile
                raise ProfileError([f"no such profile (no profiles configured)"], profile=name)
            raise ProfileError(
                [f"no such profile; available: {sorted(str(k) for k in profiles)}"], profile=name
            )
        profile = parse_profile(name, profiles[name], backend_kinds=backend_kinds)
        profile.warnings.extend(section_warnings)
        return profile

    if not profiles:
        profile = _builtin_fallback(backend_kinds)
        profile.warnings.extend(section_warnings)
        return profile

    raise ProfileError(
        [f"multiple profiles configured; set omni.default_profile or pass a name (have {sorted(str(k) for k in profiles)})"]
    )
