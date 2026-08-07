from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import AgentStore, agent_database_path
from .model import validate_adapter_name
from .policy import PermissionStore, amesh_database_path


class PlatformAdapter(ABC):
    """Contract every Amesh platform adapter implements.

    An adapter owns the platform-specific projection of raw activity into the
    Amesh social vocabulary. It never creates an agent identity or grants a
    capability of its own. Shared actor rules and agent grants are applied by
    the Amesh base layer.
    """

    name = ""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self._permissions = PermissionStore(amesh_database_path(self.home))
        self._agents = AgentStore(agent_database_path(self.home))

    @abstractmethod
    def descriptor(self) -> dict[str, Any]:
        """Non-secret configuration summary for the management plane."""

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Ledger counts, runtime state, and policy health."""

    @abstractmethod
    def actor(self, actor_key: str) -> dict[str, Any]:
        """One actor's evidence, evaluation, and effective permission view."""

    @abstractmethod
    def set_labels(
        self,
        actor_key: str,
        *,
        add: set[str] | frozenset[str],
        remove: set[str] | frozenset[str],
        source: str = "operator",
    ) -> dict[str, Any]:
        """Update operator labels in the adapter ledger."""

    @abstractmethod
    def project(self, *, limit: int = 1000) -> dict[str, Any]:
        """Fold durable ledger events into the observer-local relationship book."""

    @abstractmethod
    def reply(self, event_key: str, content: str) -> dict[str, Any]:
        """Send one operator reply after the adapter's gates pass."""

    @abstractmethod
    def relation(self, actor_key: str) -> dict[str, Any]:
        """Map one platform actor key to its observer-local relationship record."""

    def permitted_actions(
        self,
        actor_key: str,
        allowed_actions: list[str] | tuple[str, ...],
    ) -> tuple[list[str], list[str]]:
        """Apply operator permission rules to an evidence-derived action list."""
        return self._permissions.apply_allowed(
            self.name,
            actor_key,
            allowed_actions,
        )

    def permission_denies(self, actor_key: str, action: str) -> bool:
        rule = self._permissions.effective(self.name, actor_key, action)
        return rule is not None and rule.effect == "deny"

    def agent_allows(self, agent_id: str, action: str) -> bool:
        if str(agent_id).strip().lower() == "operator":
            return True
        return self._agents.authorize(agent_id, self.name, action)

    def require_agent(self, agent_id: str, action: str, *, token: str = "") -> None:
        normalized = str(agent_id).strip().lower()
        if normalized != "operator":
            record = self._agents.authenticate(token)
            if record.agent_id != normalized:
                raise PermissionError(
                    "Amesh agent token does not match the requested agent"
                )
        if not self.agent_allows(normalized, action):
            raise PermissionError(
                f"agent {normalized[:64]!r} has no {action} grant for {self.name}"
            )

    def record_permission_decision(
        self,
        actor_key: str,
        action: str,
        effect: str,
        *,
        event_key: str = "",
    ) -> None:
        self._permissions.record_decision(
            self.name,
            actor_key,
            action,
            effect,
            event_key=event_key,
        )

    def permission_rules(self, actor_key: str = "") -> list[dict[str, Any]]:
        return [
            rule.to_dict()
            for rule in self._permissions.rules(
                adapter=self.name,
                actor_key=actor_key,
            )
        ]

    def poll_once(
        self,
        queue_signal: Callable[[str, str, dict[str, Any]], str] | None = None,
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(f"{self.name} adapter has no polling loop")

    def setup(self) -> dict[str, Any]:
        raise NotImplementedError(f"{self.name} adapter has no setup defaults")

    def inject(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError(f"{self.name} adapter cannot inject messages")

    async def run(
        self,
        stop: Any,
        queue_signal: Callable[[str, str, dict[str, Any]], str],
        project_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        raise NotImplementedError(f"{self.name} adapter has no background loop")

    def close(self) -> None:
        self._permissions.close()
        self._agents.close()


def builtin_adapter_names() -> list[str]:
    return ["discord", "loopback"]


_REGISTRY: dict[str, Callable[[Path], PlatformAdapter]] = {}


def register_adapter(
    name: str,
    factory: Callable[[Path], PlatformAdapter],
) -> None:
    """Register an external adapter factory for an Amesh home.

    External adapter packages call this once at import time so the CLI, MCP,
    connector, and serve can discover them without the core importing another
    application's models. A name that already belongs to a built-in or
    registered adapter is rejected.
    """
    name = validate_adapter_name(name)
    if name in builtin_adapter_names() or name in _REGISTRY:
        raise ValueError(f"Amesh adapter already exists: {name}")
    if not callable(factory):
        raise TypeError("Amesh adapter factory must be callable")
    _REGISTRY[name] = factory


def discovered_adapter_names() -> list[str]:
    names = set(_REGISTRY)
    for entry in _entry_points():
        names.add(str(entry.name))
    return sorted(names)


def adapter_names() -> list[str]:
    return sorted(set(builtin_adapter_names()) | set(discovered_adapter_names()))


def load_adapter(home: Path, name: str) -> PlatformAdapter:
    """Load one built-in, registered, or entry-point adapter for an Amesh home."""
    name = validate_adapter_name(name)
    if name == "discord":
        from .adapters.discord import DiscordAdapter

        return DiscordAdapter(home)
    if name == "loopback":
        from .adapters.loopback import LoopbackAdapter

        return LoopbackAdapter(home)
    factory = _REGISTRY.get(name)
    if factory is not None:
        return _as_adapter(factory(home), name)
    entry = _find_entry_point(name)
    if entry is not None:
        loaded = entry.load()
        return _as_adapter(loaded(home), name)
    raise KeyError(f"unknown Amesh adapter: {name}")


def _as_adapter(value: Any, name: str) -> PlatformAdapter:
    if not isinstance(value, PlatformAdapter):
        raise TypeError(f"adapter {name!r} did not produce a PlatformAdapter")
    return value


def _entry_points() -> list[Any]:
    try:
        from importlib.metadata import entry_points

        return list(entry_points(group="amesh.adapters"))
    except Exception:  # pragma: no cover - metadata is best-effort
        return []


def _find_entry_point(name: str) -> Any | None:
    for entry in _entry_points():
        if str(entry.name) == name:
            return entry
    return None
