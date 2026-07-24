from __future__ import annotations

import pytest

from anet.scheduling import AdaptiveSchedule


def test_schedule_applies_jitter_and_bounded_idle_backoff() -> None:
    samples = iter((0.0, 0.5, 1.0, 0.5))
    schedule = AdaptiveSchedule(
        base_interval=10,
        jitter=0.25,
        max_backoff=4,
        sample=lambda: next(samples),
    )

    assert schedule.due(0)
    assert schedule.record(0, activity=False) == pytest.approx(7.5)
    assert not schedule.due(7.49)
    assert schedule.due(7.5)
    assert schedule.record(10, activity=False) == pytest.approx(20.0)
    assert schedule.record(30, activity=False) == pytest.approx(50.0)
    assert schedule.idle_rounds == 3

    assert schedule.record(80, activity=True) == pytest.approx(10.0)
    assert schedule.idle_rounds == 0
    assert schedule.snapshot(85)["next_due_seconds"] == pytest.approx(5.0)


def test_new_work_or_force_bypasses_but_does_not_destroy_schedule() -> None:
    schedule = AdaptiveSchedule(base_interval=60, jitter=0, max_backoff=4)
    schedule.record(10, activity=False)
    assert not schedule.due(20)
    assert schedule.due(20, newly_pending=True)
    assert schedule.due(20, force=True)
    assert schedule.next_due == pytest.approx(70)

    schedule.reset_backoff()
    assert schedule.record(20, activity=False, base_interval=5) == pytest.approx(5)
    assert schedule.next_due == pytest.approx(25)
