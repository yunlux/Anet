from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Mapping

from .model import validate_adapter_name
from .policy import PermissionStore, amesh_database_path


class PlatformAdapter(ABC):
    """Contract every Amesh platform adapter implements.

    An adapter owns the platform-specific projection of raw activity into the
    Anet social vocabulary and never creates an identity root, social graph,
    or authorization decision of its own. Operator permission rules are
    provided by the shared base so every adapter applies the same overlay.
    """

    name = ""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self._permissions = PermissionStore(amesh_database_path(self.home))

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


def builtin_adapter_names() -> list[str]:
    return ["discord", "loopback"]


def load_adapter(home: Path, name: str) -> PlatformAdapter:
    """Load one built-in adapter by name for a node home."""
    name = validate_adapter_name(name)
    if name == "discord":
        from .adapters.discord import DiscordAdapter

        return DiscordAdapter(home)
    if name == "loopback":
        from .adapters.loopback import LoopbackAdapter

        return LoopbackAdapter(home)
    raise KeyError(f"unknown Amesh adapter: {name}")
