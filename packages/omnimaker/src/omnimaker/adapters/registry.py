"""Central registry for node adapters and platform bindings."""

from __future__ import annotations

from omnimaker.adapters.base import NodeAdapter


class AdapterRegistry:
    """Maps ``use:`` references to ``NodeAdapter`` subclasses."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[NodeAdapter]] = {}
        self._bindings: dict[str, type[NodeAdapter]] = {}

    # ── Node adapters ──────────────────────────────────────────────────

    def register(self, ref: str, cls: type[NodeAdapter]) -> None:
        """Register a node adapter under a ``use:`` reference string."""
        if not issubclass(cls, NodeAdapter):
            raise TypeError(f"{cls} must inherit from NodeAdapter")
        self._adapters[ref] = cls

    def resolve(self, ref: str) -> type[NodeAdapter]:
        """Look up a node adapter by reference string."""
        cls = self._adapters.get(ref)
        if cls is None:
            raise KeyError(f"No adapter registered for '{ref}'")
        return cls

    def unregister(self, ref: str) -> None:
        self._adapters.pop(ref, None)

    def list(self) -> list[str]:
        return sorted(self._adapters)

    # ── Platform bindings ──────────────────────────────────────────────

    def register_binding(self, name: str, cls: type[NodeAdapter],
                         config: dict | None = None) -> None:
        """Register a platform binding adapter under a ``@name`` prefix."""
        if not issubclass(cls, NodeAdapter):
            raise TypeError(f"{cls} must inherit from NodeAdapter")
        self._bindings[name] = cls

    def resolve_binding(self, name: str) -> type[NodeAdapter]:
        cls = self._bindings.get(name)
        if cls is None:
            raise KeyError(f"No binding registered for '@{name}'")
        return cls

    def list_bindings(self) -> list[str]:
        return sorted(self._bindings)

    # ── Built-in registration ──────────────────────────────────────────

    def register_builtins(self) -> None:
        """Register engine-native routing primitives."""
        from omnimaker.adapters.primitives import (
            BufferAdapter,
            DebounceAdapter,
            FilterAdapter,
            MergeAdapter,
            TeeAdapter,
            ThrottleAdapter,
        )
        self.register("omni/merge", MergeAdapter)
        self.register("omni/tee", TeeAdapter)
        self.register("omni/filter", FilterAdapter)
        self.register("omni/buffer", BufferAdapter)
        self.register("omni/debounce", DebounceAdapter)
        self.register("omni/throttle", ThrottleAdapter)


# Global singleton
registry = AdapterRegistry()