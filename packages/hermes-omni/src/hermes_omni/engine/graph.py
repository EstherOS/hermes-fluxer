"""Component graph — parse, validate, infer tempo/mode/core (omni config v2).

A profile is a graph of *components*, each a model or service with declared
inputs (``ins``), outputs (``outs``), and exposed tools (``tools``).  The
graph supports two interaction mechanisms:

* **push** — data flows automatically from source → dest(s) when a component
  produces output of a given sense.
* **pull** — a component calls another component's tool on demand.

Every connection has a **mode** (live / tape / frame) and an **interrupt
policy** (barge-in true/false).  Tempo and profile mode are *inferred* from
the io-mode mix, not declared explicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..types import ProfileError, Sense

__all__ = [
    "DEFAULT_IO",
    "Component",
    "ComponentGraph",
    "ComponentGraphProfile",
    "EdgeProperties",
    "PushRoute",
    "parse_component_config",
]

# ── defaults ─────────────────────────────────────────────────────────────────

#: Default io settings per sense (from the spec's table).
DEFAULT_IO: dict[str, dict[str, Any]] = {
    "audio": {"mode": "live", "interrupt": True},
    "video": {"mode": "tape", "interrupt": False},
    "image": {"mode": "frame", "interrupt": False},
    "text": {"mode": "live", "interrupt": False},
}


# ── data classes ──────────────────────────────────────────────────────────────


@dataclass
class EdgeProperties:
    """Per-connection io properties (mode + interrupt policy)."""

    mode: str = "live"
    interrupt: bool = True

    @classmethod
    def from_config(cls, cfg: Any) -> EdgeProperties:
        if not isinstance(cfg, Mapping):
            return cls()
        mode = str(cfg.get("mode", "live")).strip().lower()
        if mode not in ("live", "tape", "frame"):
            raise ProfileError([f"unknown io mode {cfg.get('mode')!r}; expected live/tape/frame"])
        interrupt = bool(cfg.get("interrupt", True))
        return cls(mode=mode, interrupt=interrupt)


@dataclass
class PushRoute:
    """One directed data edge in the component graph."""

    source: str
    dest: str
    sense: str
    mode: str = "live"
    interrupt: bool = True


@dataclass
class Component:
    """One node in the component graph.

    ``ins`` maps sense → list of source names (``"user"`` or component names).
    ``outs`` maps sense → list of destination names.
    ``tools`` maps tool-name → target (component name or ``"core"``).
    ``io`` maps sense → raw config dict for per-connection overrides.
    """

    name: str
    ins: dict[str, list[str]] = field(default_factory=dict)
    outs: dict[str, list[str]] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    io: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _io_for(self, sense: str, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve effective io settings for *sense* (config override → default)."""
        sense = sense.strip().lower()
        if sense in self.io:
            return self.io[sense]
        if fallback is not None:
            return fallback
        return dict(DEFAULT_IO.get(sense, {"mode": "live", "interrupt": False}))


@dataclass
class ComponentGraph:
    """Parsed, validated component graph for a v2 profile.

    Attributes
    ----------
    components
        All named components in the graph.
    routes
        Resolved push routes — fully expanded from ``outs`` / ``routes``
        overrides.
    """

    components: dict[str, Component] = field(default_factory=dict)
    routes: list[PushRoute] = field(default_factory=list)

    # ── public API ──────────────────────────────────────────────────────────

    def has_component(self, name: str) -> bool:
        return name in self.components

    def component_names(self) -> list[str]:
        return sorted(self.components)

    def get_component(self, name: str) -> Component | None:
        return self.components.get(name)

    # ── build / validate ────────────────────────────────────────────────────

    def build(self) -> None:
        """Validate the graph: no cycles, all sources/dests exist, senses match.

        Raises :class:`ProfileError` on any issue.  Also populates
        ``self.routes`` from the components' ``outs`` declarations.
        """
        errors: list[str] = []
        valid_senses = {s.value for s in Sense}
        # We'll collect names that exist
        all_names: set[str] = set(self.components)

        for cname, comp in self.components.items():
            # Validate ins
            for sense, sources in comp.ins.items():
                if sense not in valid_senses:
                    errors.append(
                        f"component {cname!r}: unknown sense {sense!r} in ins; "
                        f"expected one of {sorted(valid_senses)}"
                    )
                    continue
                for src in sources:
                    if src == "user":
                        continue
                    if src not in all_names:
                        errors.append(
                            f"component {cname!r}: ins source {src!r} for sense "
                            f"{sense!r} is not a component name; known: {sorted(all_names)}"
                        )

            # Validate outs
            for sense, dests in comp.outs.items():
                if sense not in valid_senses:
                    errors.append(
                        f"component {cname!r}: unknown sense {sense!r} in outs; "
                        f"expected one of {sorted(valid_senses)}"
                    )
                    continue
                for dest in dests:
                    if dest == "user":
                        continue
                    if dest not in all_names:
                        errors.append(
                            f"component {cname!r}: outs dest {dest!r} for sense "
                            f"{sense!r} is not a component name; known: {sorted(all_names)}"
                        )

            # Validate tools targets — they can be "core", a component name,
            # or an arbitrary capability name (not a component).  No error.
            for target in comp.tools.values():
                if target == "core":
                    continue
                if target in all_names:
                    continue
                # Anything else is treated as an external capability — no error

            # Validate io sense keys
            for io_sense in comp.io:
                if io_sense not in valid_senses:
                    errors.append(
                        f"component {cname!r}: io key {io_sense!r} is not a valid sense; "
                        f"expected one of {sorted(valid_senses)}"
                    )
                mode_str = str(comp.io[io_sense].get("mode", "")).strip().lower()
                if mode_str and mode_str not in ("live", "tape", "frame"):
                    errors.append(
                        f"component {cname!r}: io mode {mode_str!r} for {io_sense!r} "
                        f"not in (live, tape, frame)"
                    )

        # ── Cycle detection (simplified: DFS) ───────────────────────────────
        if all_names:
            visited: set[str] = set()
            stack: set[str] = set()

            def _dfs(c: str) -> None:
                if c in stack:
                    errors.append(f"cycle detected involving component {c!r}")
                    return
                if c in visited:
                    return
                visited.add(c)
                stack.add(c)
                comp = self.components.get(c)
                if comp:
                    for dests in comp.outs.values():
                        for d in dests:
                            if d in all_names and d not in visited:
                                _dfs(d)
                stack.discard(c)

            for n in all_names:
                if n not in visited:
                    _dfs(n)

        if errors:
            raise ProfileError(errors)

        # ── Build routes ───────────────────────────────────────────────────
        self.routes = []
        for cname, comp in self.components.items():
            for sense, dests in comp.outs.items():
                io_defaults = DEFAULT_IO.get(sense, {"mode": "live", "interrupt": False})
                for dest in dests:
                    props = EdgeProperties.from_config(comp._io_for(sense, io_defaults))
                    self.routes.append(
                        PushRoute(
                            source=cname,
                            dest=dest,
                            sense=sense,
                            mode=props.mode,
                            interrupt=props.interrupt,
                        )
                    )

    # ── graph endpoint helpers ─────────────────────────────────────────────

    def audio_input_component(self) -> str | None:
        """Find the component that receives ``audio`` from ``user`` in its ``ins``.

        Returns ``None`` if no component declares ``audio: [user]``.
        """
        for cname, comp in self.components.items():
            for sense, sources in comp.ins.items():
                if sense == "audio" and "user" in sources:
                    return cname
        return None

    def audio_output_component(self) -> str | None:
        """Find the component that sends ``audio`` to ``user`` in its ``outs``.

        Returns ``None`` if no component declares ``audio: [user]``.
        """
        for cname, comp in self.components.items():
            for sense, dests in comp.outs.items():
                if sense == "audio" and "user" in dests:
                    return cname
        return None

    def text_input_component(self) -> str | None:
        """Find the component that receives ``text`` from ``user`` in its ``ins``.

        Returns ``None`` if no component declares ``text: [user]``.
        """
        for cname, comp in self.components.items():
            for sense, sources in comp.ins.items():
                if sense == "text" and "user" in sources:
                    return cname
        return None

    # ── inference ───────────────────────────────────────────────────────────

    def infer_tempo(self, component: str) -> str:
        """Infer a component's tempo from its io-mode mix.

        Only explicit io overrides contribute to the tempo inference.
        A component without any io config gets its tempo from the default
        modes of its connected senses.

        Returns ``"realtime"``, ``"fast_half_duplex"``, or ``"turn"``.
        """
        comp = self.components.get(component)
        if not comp:
            return "turn"

        # Collect all senses that have connections (ins or outs)
        connected_senses: set[str] = set()
        for sense in comp.ins:
            connected_senses.add(sense)
        for sense in comp.outs:
            connected_senses.add(sense)

        if not connected_senses:
            return "turn"

        # Determine the effective mode for each connected sense.
        # If a sense has an explicit io override, it contributes to the mix.
        # If no sense has an explicit override, fall back to defaults for all.
        modes: set[str] = set()
        has_explicit_io = bool(comp.io)

        if has_explicit_io:
            # Only senses with explicit io config contribute
            for sense in connected_senses:
                if sense in comp.io:
                    cfg = comp.io[sense]
                    mode = str(cfg.get("mode", DEFAULT_IO.get(sense, {}).get("mode", "live"))).strip().lower()
                    if mode in ("live", "tape", "frame"):
                        modes.add(mode)
                # Senses without explicit io don't affect tempo
        else:
            # No explicit io: use defaults for all connected senses
            for sense in connected_senses:
                mode = DEFAULT_IO.get(sense, {}).get("mode", "live")
                if mode in ("live", "tape", "frame"):
                    modes.add(mode)

        if not modes:
            return "turn"
        if modes == {"live"}:
            return "realtime"
        if modes.issubset({"tape", "frame"}):
            return "turn"
        return "fast_half_duplex"

    def infer_core(self) -> str | None:
        """Identify the component that has ``harness: core`` in its tools."""
        for cname, comp in self.components.items():
            if comp.tools.get("harness") == "core":
                return cname
        return None

    def core_component(self) -> str | None:
        """Alias for :meth:`infer_core` — component with ``harness: core``."""
        return self.infer_core()

    def infer_profile_mode(self) -> str:
        """Infer the profile's emergent mode label.

        Returns ``"unified"``, ``"stitched"``, ``"talker-thinker"``,
        ``"router"``, or ``"glados"``.
        """
        names = list(self.components)
        if len(names) == 1:
            return "unified"

        # Four+ components → glados (full frankenstein)
        if len(names) >= 4:
            return "glados"

        # Router: at least one component has no ``user`` in its outs
        for comp in self.components.values():
            all_dests = {d for dests in comp.outs.values() for d in dests}
            if "user" not in all_dests:
                return "router"

        # Talker-thinker: exactly 2 components, bidirectional text link
        if len(names) == 2:
            a, b = names
            ca, cb = self.components[a], self.components[b]
            a_to_b = any(b in dests for dests in ca.outs.values() for dest in dests)
            b_to_a = any(a in dests for dests in cb.outs.values() for dest in dests)
            if a_to_b and b_to_a:
                return "talker-thinker"
            return "stitched"

        return "stitched"


@dataclass
class ComponentGraphProfile:
    """A v2 profile backed by a :class:`ComponentGraph`.

    This coexists with :class:`~hermes_omni.profiles.ResolvedProfile`.
    The session controller checks ``isinstance`` to pick the wiring path.
    """

    name: str
    graph: ComponentGraph
    warnings: list[str] = field(default_factory=list)
    source: str = "config"


# ── config parsing ───────────────────────────────────────────────────────────




def _normalize_single_model_shorthand(name: str, spec: Any) -> dict[str, Any]:
    """Handle ``brain: {model: <backend>, senses: [<sense>, ...]}`` shorthand."""
    if not isinstance(spec, Mapping):
        return {"components": spec} if "components" in str(spec) else spec
    # Check if this is the shorthand form (has 'model' key at component level)
    if "model" in spec and not isinstance(spec.get("model"), Mapping):
        senses = spec.get("senses", [])
        if not isinstance(senses, (list, tuple)):
            senses = []
        ins = {sense: ["user"] for sense in senses}
        outs = {sense: ["user"] for sense in senses if sense in ("text", "audio")}
        tools = spec.get("tools", {})
        io_cfg = spec.get("io", {})
        return {
            name: {
                "ins": ins,
                "outs": outs,
                "tools": tools,
                "io": io_cfg,
            }
        }
    return {name: dict(spec)}


def parse_component_config(
    name: str,
    spec: Mapping[str, Any],
) -> ComponentGraphProfile:
    """Parse a profile spec with the ``components`` grammar into a :class:`ComponentGraphProfile`.

    Parameters
    ----------
    name
        Profile name (for error context).
    spec
        The profile mapping — must contain a ``components`` key.

    Raises
    ------
    ProfileError
        On missing/unparseable ``components`` section.
    """
    if not isinstance(spec, Mapping):
        raise ProfileError([f"profile spec must be a mapping, got {type(spec).__name__}"], profile=name)

    raw_components = spec.get("components")
    if not raw_components:
        raise ProfileError(
            [f"v2 profile requires a 'components' section, got {type(raw_components).__name__}"],
            profile=name,
        )
    if not isinstance(raw_components, Mapping) or not raw_components:
        raise ProfileError(
            ["'components' must be a non-empty mapping of component names → config"],
            profile=name,
        )

    warnings: list[str] = []
    parsed: dict[str, Component] = {}

    for cname, cfg in raw_components.items():
        cname = str(cname)
        if not isinstance(cfg, Mapping):
            warnings.append(f"component {cname!r}: skipping non-mapping config ({type(cfg).__name__})")
            continue

        ins: dict[str, list[str]] = {}
        raw_ins = cfg.get("ins") or {}
        if isinstance(raw_ins, Mapping):
            for sense, sources in raw_ins.items():
                sense_str = str(sense).strip().lower()
                if isinstance(sources, (list, tuple)):
                    ins[sense_str] = [str(s).strip() for s in sources]
                elif isinstance(sources, str):
                    ins[sense_str] = [sources]
                else:
                    warnings.append(
                        f"component {cname!r}: ins.{sense!r} is not a list; skipping"
                    )

        outs: dict[str, list[str]] = {}
        raw_outs = cfg.get("outs") or {}
        if isinstance(raw_outs, Mapping):
            for sense, dests in raw_outs.items():
                sense_str = str(sense).strip().lower()
                if isinstance(dests, (list, tuple)):
                    outs[sense_str] = [str(d).strip() for d in dests]
                elif isinstance(dests, str):
                    outs[sense_str] = [dests]
                else:
                    warnings.append(
                        f"component {cname!r}: outs.{sense!r} is not a list; skipping"
                    )

        tools: dict[str, str] = {}
        raw_tools = cfg.get("tools") or {}
        if isinstance(raw_tools, Mapping):
            for tname, target in raw_tools.items():
                tools[str(tname).strip()] = str(target).strip()

        io_cfg: dict[str, dict[str, Any]] = {}
        raw_io = cfg.get("io") or {}
        if isinstance(raw_io, Mapping):
            for io_sense, iocfg in raw_io.items():
                sense_str = str(io_sense).strip().lower()
                if isinstance(iocfg, Mapping):
                    io_cfg[sense_str] = dict(iocfg)
                else:
                    warnings.append(f"component {cname!r}: io.{io_sense!r} is not a mapping; skipping")

        parsed[cname] = Component(
            name=cname,
            ins=ins,
            outs=outs,
            tools=tools,
            io=io_cfg,
        )

    # ── Build and validate the graph ──────────────────────────────────────
    graph = ComponentGraph(components=parsed)
    graph.build()

    return ComponentGraphProfile(
        name=name,
        graph=graph,
        warnings=warnings,
        source="config",
    )