from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def _system_sample() -> float:
    return random.SystemRandom().random()


@dataclass
class AdaptiveSchedule:
    """Monotonic, jittered schedule with bounded exponential idle backoff."""

    base_interval: float
    jitter: float = 0.25
    max_backoff: float = 4.0
    sample: Callable[[], float] = field(default=_system_sample, repr=False)
    idle_rounds: int = 0
    next_due: float = 0.0
    last_delay: float = 0.0

    def __post_init__(self) -> None:
        self.base_interval = float(self.base_interval)
        self.jitter = float(self.jitter)
        self.max_backoff = float(self.max_backoff)
        if self.base_interval <= 0:
            raise ValueError("schedule interval must be positive")
        if not 0.0 <= self.jitter <= 0.9:
            raise ValueError("schedule jitter must be between 0 and 0.9")
        if self.max_backoff < 1.0:
            raise ValueError("schedule max backoff must be at least 1")

    def due(self, now: float, *, force: bool = False, newly_pending: bool = False) -> bool:
        return bool(force or newly_pending or float(now) >= self.next_due)

    def reset_backoff(self) -> None:
        self.idle_rounds = 0

    def record(
        self,
        now: float,
        *,
        activity: bool,
        base_interval: float | None = None,
    ) -> float:
        if activity:
            self.idle_rounds = 0
        else:
            self.idle_rounds = min(self.idle_rounds + 1, 1_000_000)
        exponent = min(max(0, self.idle_rounds - 1), 30)
        multiplier = min(self.max_backoff, float(2**exponent))
        sample = max(0.0, min(1.0, float(self.sample())))
        jitter_factor = (1.0 - self.jitter) + (2.0 * self.jitter * sample)
        interval = self.base_interval if base_interval is None else float(base_interval)
        if interval <= 0:
            raise ValueError("schedule interval override must be positive")
        self.last_delay = max(0.05, interval * multiplier * jitter_factor)
        self.next_due = float(now) + self.last_delay
        return self.last_delay

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "idle_rounds": self.idle_rounds,
            "last_delay_seconds": round(self.last_delay, 3),
            "next_due_seconds": round(max(0.0, self.next_due - float(now)), 3),
            "base_interval_seconds": self.base_interval,
            "jitter": self.jitter,
            "max_backoff": self.max_backoff,
        }
