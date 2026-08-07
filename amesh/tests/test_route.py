from __future__ import annotations

import time

import pytest

from amesh.route import (
    MAX_ATTEMPTS,
    RouteStore,
    route_database_path,
)
from amesh.signal import build_signal


def _signal(*, source="a", actor="b", expires_ms=0) -> dict:
    created = int(time.time() * 1000)
    return build_signal(
        platform="loopback",
        adapter="loopback-spool-v1",
        source_event_id=(source * 32),
        actor_key=(actor * 64),
        created_ms=created,
        expires_ms=expires_ms or created + 10_000_000,
        content_level="mention",
        content="hi @amesh",
        labels=set(),
        evaluation={
            "action": "surface",
            "allowed_actions": ["observe", "surface"],
            "reasons": ["test"],
            "policy_version": 1,
            "reputation": {
                "score": 50,
                "raw_score": 50,
                "confidence": 0,
                "algorithm": "amesh-evidence-v1",
            },
        },
        provenance={},
    )


def _failing_deliver(_body):
    raise RuntimeError("boom")


def test_enqueue_and_dedup(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        signal = _signal()
        first = store.enqueue(
            "review-agent", "loopback", "amesh.loopback.signal", signal
        )
        assert first["duplicate"] is False
        assert first["state"] == "pending"
        assert len(first["route_id"]) == 32
        second = store.enqueue(
            "review-agent", "loopback", "amesh.loopback.signal", signal
        )
        assert second["duplicate"] is True
        assert second["route_id"] == first["route_id"]
        assert store.status()["pending"] == 1
    finally:
        store.close()


def test_deliver_marks_delivered(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        signal = _signal()
        route = store.enqueue(
            "review-agent", "loopback", "amesh.loopback.signal", signal
        )
        delivered: list[dict] = []
        result = store.deliver_due(lambda body: delivered.append(body))
        assert result["delivered"] == 1
        assert delivered == [signal]
        assert store.status()["delivered"] == 1
        assert store.status()["pending"] == 0
        assert store.list(state="delivered")[0]["route_id"] == route["route_id"]
    finally:
        store.close()


def test_retry_with_backoff_and_failure(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        store.enqueue("review-agent", "loopback", "amesh.loopback.signal", _signal())
        now = int(time.time() * 1000)
        for attempt in range(1, MAX_ATTEMPTS):
            result = store.deliver_due(_failing_deliver, now_ms=now)
            assert result["failed"] == 1
            state = store.status()
            assert state["retrying"] == 1
            assert state["pending"] == 0
            route = store.list(state="retrying")[0]
            assert route["attempts"] == attempt
            assert route["next_retry_ms"] > now
            now = route["next_retry_ms"]
        result = store.deliver_due(_failing_deliver, now_ms=now)
        assert result["failed"] == 1
        assert store.status()["failed"] == 1
    finally:
        store.close()


def test_retry_resets_failed_route(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        route = store.enqueue(
            "review-agent", "loopback", "amesh.loopback.signal", _signal()
        )
        now = int(time.time() * 1000)
        for _ in range(MAX_ATTEMPTS):
            result = store.deliver_due(_failing_deliver, now_ms=now)
            assert result["failed"] == 1
            now = (
                store.list(state="retrying")[0]["next_retry_ms"]
                if store.status()["retrying"]
                else now
            )
        assert store.status()["failed"] == 1
        store.retry(route["route_id"])
        assert store.status()["retrying"] == 1
        assert store.status()["failed"] == 0
        result = store.deliver_due(lambda body: None, now_ms=int(time.time() * 1000))
        assert result["delivered"] == 1
    finally:
        store.close()


def test_expired_route_is_not_delivered(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        created = int(time.time() * 1000)
        store.enqueue(
            "review-agent",
            "loopback",
            "amesh.loopback.signal",
            _signal(expires_ms=created + 100),
        )
        result = store.deliver_due(lambda body: None, now_ms=created + 200)
        assert result["expired"] == 1
        assert result["delivered"] == 0
        assert store.status()["expired"] == 1
    finally:
        store.close()


def test_destination_policy_denies_route(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        store.set_policy("blocked-agent", "loopback", False)
        with pytest.raises(ValueError, match="policy denies"):
            store.enqueue(
                "blocked-agent", "loopback", "amesh.loopback.signal", _signal()
            )
        store.set_policy("blocked-agent", "loopback", True)
        route = store.enqueue(
            "blocked-agent", "loopback", "amesh.loopback.signal", _signal()
        )
        assert route["duplicate"] is False
        rules = store.policy_rules()
        assert len(rules) == 1
        assert rules[0]["destination"] == "blocked-agent"
        assert rules[0]["allowed"] is True
    finally:
        store.close()


def test_fail_closed_when_no_default(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path), default_allow=False)
    try:
        with pytest.raises(ValueError, match="policy denies"):
            store.enqueue(
                "unknown-agent", "loopback", "amesh.loopback.signal", _signal()
            )
        store.set_policy("known-agent", "loopback", True)
        assert (
            store.enqueue(
                "known-agent", "loopback", "amesh.loopback.signal", _signal()
            )["state"]
            == "pending"
        )
    finally:
        store.close()


def test_enqueue_validates_signal(tmp_path) -> None:
    store = RouteStore(route_database_path(tmp_path))
    try:
        with pytest.raises(ValueError, match="signal_id"):
            store.enqueue(
                "review-agent", "loopback", "amesh.loopback.signal", {"no": "id"}
            )
        with pytest.raises(ValueError, match="destination"):
            store.enqueue("", "loopback", "amesh.loopback.signal", _signal())
    finally:
        store.close()
