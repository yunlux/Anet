from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest


NOW = 1_750_000_000_000
SCRIPT = Path(__file__).resolve().parents[1] / "experiments" / "abazr_demo.py"
SPEC = importlib.util.spec_from_file_location("abazr_demo", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
Bazaar = MODULE.Bazaar
Participant = MODULE.Participant
run_demo = MODULE.run_demo


def test_abazr_demo_completes_non_financial_vertical_slice() -> None:
    records = run_demo(now_ms=NOW)
    assert [record.kind for record in records] == [
        "aba.need.v1",
        "aba.offer.v1",
        "aba.match.v1",
        "aba.proposal.v1",
        "aba.agreement.v1",
        "aba.fulfillment.v1",
        "aba.evidence.v1",
    ]
    assert [record.visibility for record in records] == [
        "public",
        "public",
        "private",
        "private",
        "private",
        "private",
        "private",
    ]
    assert records[2].payload["authorized"] is False
    assert records[-1].payload["outcome"] == "accepted"
    for record in records:
        record.verify(now_ms=NOW)
        assert not {
            "buyer",
            "seller",
            "price",
            "currency",
            "token",
            "wallet",
            "chain_id",
            "escrow",
            "payment",
        } & set(record.payload)


def test_abazr_rejects_signed_record_tampering() -> None:
    need = run_demo(now_ms=NOW)[0]
    tampered = replace(
        need,
        payload={**need.payload, "title": "tampered"},
    )
    with pytest.raises(ValueError, match="invalid ABA record signature"):
        tampered.verify(now_ms=NOW)


def test_abazr_match_is_explainable_but_never_authorizing() -> None:
    match = run_demo(now_ms=NOW)[2]
    assert match.payload["score"] == 95
    assert match.payload["reasons"] == [
        "capability:protocol.review",
        "tag:agent-infrastructure",
        "tag:python",
        "tag:security",
    ]
    assert match.payload["authorized"] is False


def test_abazr_does_not_match_different_capabilities() -> None:
    requester = Participant.generate("requester")
    provider = Participant.generate("provider")
    bazaar = Bazaar(Participant.generate("matcher"))
    for participant, kind, capability in (
        (requester, "aba.need.v1", "protocol.review"),
        (provider, "aba.offer.v1", "artifact.render"),
    ):
        bazaar.submit(
            participant.issue(
                kind,
                visibility="public",
                payload={
                    "title": capability,
                    "summary": "Public-safe summary.",
                    "capability": capability,
                    "tags": ["python"],
                },
                now_ms=NOW,
            ),
            now_ms=NOW,
        )
    assert bazaar.find_matches(now_ms=NOW) == []


def test_abazr_d0_does_not_index_circle_records_without_membership() -> None:
    requester = Participant.generate("requester")
    provider = Participant.generate("provider")
    bazaar = Bazaar(Participant.generate("matcher"))
    for participant, kind in (
        (requester, "aba.need.v1"),
        (provider, "aba.offer.v1"),
    ):
        bazaar.submit(
            participant.issue(
                kind,
                visibility="circle",
                payload={
                    "title": "Protocol review",
                    "summary": "Circle-scoped summary.",
                    "capability": "protocol.review",
                    "tags": ["python"],
                },
                now_ms=NOW,
            ),
            now_ms=NOW,
        )
    assert bazaar.find_matches(now_ms=NOW) == []


def test_abazr_rejects_financial_fields_without_adapter() -> None:
    requester = Participant.generate("requester")
    bazaar = Bazaar(Participant.generate("matcher"))
    record = requester.issue(
        "aba.need.v1",
        visibility="public",
        payload={
            "title": "Review",
            "summary": "Public-safe summary.",
            "capability": "protocol.review",
            "tags": ["python"],
            "price": 1,
        },
        now_ms=NOW,
    )
    with pytest.raises(ValueError, match="Settlement Adapter"):
        bazaar.submit(record, now_ms=NOW)
