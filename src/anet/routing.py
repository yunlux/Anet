from __future__ import annotations

from dataclasses import dataclass

from .config import (
    AhubCarrierConfig,
    DirectoryCarrierConfig,
    RoutingConfig,
    WebDAVCarrierConfig,
)
from .packet import QOS_CLASSES, now_ms
from .store import PacketStore


FAST_FAILOVER_QOS = frozenset({"control", "interactive"})


@dataclass(frozen=True)
class RouteDecision:
    peer_id: str
    selected_path: str
    reason: str
    push_qos: dict[str, frozenset[str]]
    changed: bool


class AdaptiveRouter:
    """Deterministic path selection with fast failure and slow recovery.

    Direct connectivity is still probed while a fallback is selected.  A
    single failure can race high-priority traffic onto the best asynchronous
    carrier; all traffic moves only after the configured failure threshold.
    Recovery requires multiple consecutive successes plus a cooldown, which
    prevents route flapping on unstable links.
    """

    def __init__(self, store: PacketStore, config: RoutingConfig) -> None:
        self.store = store
        self.config = config

    def _score(
        self,
        peer_id: str,
        carrier: AhubCarrierConfig | DirectoryCarrierConfig | WebDAVCarrierConfig,
    ) -> float:
        metric = self.store.path_metric(peer_id, carrier.path_id) or {}
        return (
            float(carrier.priority) * 100.0
            + float(metric.get("consecutive_failures", 0)) * 10_000.0
            + float(metric.get("ewma_rtt_ms", 0.0))
        )

    def decide(
        self,
        peer_id: str,
        *,
        has_direct: bool,
        carriers: tuple[
            AhubCarrierConfig | DirectoryCarrierConfig | WebDAVCarrierConfig,
            ...,
        ]
        | list[AhubCarrierConfig | DirectoryCarrierConfig | WebDAVCarrierConfig],
    ) -> RouteDecision:
        usable = [carrier for carrier in carriers if carrier.enabled and carrier.mode != "receive-only"]
        usable.sort(key=lambda carrier: (self._score(peer_id, carrier), carrier.name))
        healthy = [
            carrier
            for carrier in usable
            if int(
                (self.store.path_metric(peer_id, carrier.path_id) or {}).get(
                    "consecutive_failures", 0
                )
            )
            < self.config.carrier_failure_threshold
        ]
        best = healthy[0] if healthy else (usable[0] if usable else None)
        current = self.store.route(peer_id)
        current_path = str(current["selected_path"]) if current else ""
        direct_metric = self.store.path_metric(peer_id, "direct") or {}
        failures = int(direct_metric.get("consecutive_failures", 0))
        successes = int(direct_metric.get("consecutive_successes", 0))
        current_ms = now_ms()
        switched_ms = int(current.get("switched_ms", 0)) if current else 0
        cooled_down = current_ms - switched_ms >= int(self.config.switch_cooldown * 1000)
        current_carrier = next(
            (carrier for carrier in usable if carrier.path_id == current_path),
            None,
        )

        if current_path not in {"", "direct", "none"}:
            if current_carrier is None:
                selected = best.path_id if best else ("direct" if has_direct else "none")
                reason = "selected fallback disappeared"
            else:
                current_metric = self.store.path_metric(peer_id, current_path) or {}
                carrier_failures = int(current_metric.get("consecutive_failures", 0))
                alternatives = [carrier for carrier in healthy if carrier.path_id != current_path]
                if carrier_failures >= self.config.carrier_failure_threshold and alternatives:
                    selected = alternatives[0].path_id
                    reason = (
                        f"fallback {current_path} failed {carrier_failures} consecutive probes"
                    )
                elif (
                    has_direct
                    and successes >= self.config.direct_recovery_threshold
                    and cooled_down
                ):
                    selected = "direct"
                    reason = f"direct recovered for {successes} consecutive probes"
                elif (
                    best is not None
                    and best.path_id != current_path
                    and cooled_down
                    and int(
                        (self.store.path_metric(peer_id, best.path_id) or {}).get(
                            "consecutive_successes", 0
                        )
                    )
                    >= self.config.carrier_recovery_threshold
                ):
                    selected = best.path_id
                    reason = (
                        f"preferred fallback {best.path_id} recovered for "
                        f"{self.config.carrier_recovery_threshold} probes"
                    )
                else:
                    selected = current_path
                    reason = (
                        "fallback held; all alternatives unhealthy"
                        if carrier_failures >= self.config.carrier_failure_threshold
                        else "fallback held by recovery hysteresis"
                    )
        elif not has_direct:
            selected = best.path_id if best else "none"
            reason = "direct path unavailable"
        elif current_path == "none":
            if failures >= self.config.direct_failure_threshold and best:
                selected = best.path_id
                reason = f"direct failed {failures} consecutive probes"
            else:
                selected = "direct"
                reason = "direct available"
        elif failures >= self.config.direct_failure_threshold and best:
            selected = best.path_id
            reason = f"direct failed {failures} consecutive probes"
        else:
            selected = "direct"
            reason = "direct healthy or below failure threshold"

        changed = current_path != selected
        state = self.store.set_route(peer_id, selected, reason)
        selected = str(state["selected_path"])
        push: dict[str, frozenset[str]] = {}
        all_qos = frozenset(QOS_CLASSES)

        for carrier in carriers:
            if not carrier.enabled or carrier.mode == "receive-only":
                continue
            if carrier.mode == "always":
                push[carrier.name] = all_qos

        selected_carrier = next((carrier for carrier in usable if carrier.path_id == selected), None)
        if selected_carrier is not None:
            replica_candidates = [selected_carrier]
            replica_candidates.extend(
                carrier
                for carrier in (healthy or usable)
                if carrier.path_id != selected_carrier.path_id
            )
            for carrier in replica_candidates[: self.config.carrier_replica_count]:
                existing = push.get(carrier.name, frozenset())
                push[carrier.name] = frozenset(set(existing) | set(all_qos))
        elif selected == "direct" and failures >= 1 and best:
            for carrier in (healthy or usable)[: self.config.carrier_replica_count]:
                existing = push.get(carrier.name, frozenset())
                push[carrier.name] = frozenset(set(existing) | set(FAST_FAILOVER_QOS))

        return RouteDecision(peer_id, selected, reason, push, changed)
