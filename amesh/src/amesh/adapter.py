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
                raise PermissionError("Amesh agent token does not match the requested agent")
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


def load_adapter(home: Path, name: str) -> PlatformAdapter:
    """Load one built-in adapter by name for an Amesh home."""
    name = validate_adapter_name(name)
    if name == "discord":
        from .adapters.discord import DiscordAdapter

        return DiscordAdapter(home)
    if name == "loopback":
        from .adapters.loopback import LoopbackAdapter

        return LoopbackAdapter(home)
    raise KeyError(f"unknown Amesh adapter: {name}")
