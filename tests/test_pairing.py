from __future__ import annotations

import json
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidSignature

from anet.cli import main
from anet.identity import Identity
from anet.pairing import PairOffer, PairResponse


def test_pairing_offer_and_response_are_signed_and_challenge_bound() -> None:
    created = 1_800_000_000_000
    initiator = Identity.generate("initiator")
    responder = Identity.generate("responder")
    offer = PairOffer.create(
        initiator,
        initiator.card(addresses=("tls://10.0.0.1:4242",)),
        ttl_seconds=600,
        now=created,
    )
    offer.verify(now=created + 1)
    response = PairResponse.create(
        offer,
        responder,
        responder.card(addresses=("tls://10.0.0.2:4242",)),
        now=created + 2,
    )
    response.verify(offer, now=created + 3)

    with pytest.raises(InvalidSignature):
        replace(offer, expires_ms=offer.expires_ms + 1).verify(now=created + 3)

    other_offer = PairOffer.create(
        initiator,
        initiator.card(),
        ttl_seconds=600,
        now=created,
    )
    with pytest.raises(ValueError, match="not bound"):
        response.verify(other_offer, now=created + 3)


def test_pairing_rejects_expiry_and_self_pairing() -> None:
    created = 1_800_000_000_000
    identity = Identity.generate("node")
    offer = PairOffer.create(identity, identity.card(), ttl_seconds=60, now=created)
    with pytest.raises(ValueError, match="expired"):
        offer.verify(now=offer.expires_ms + 1)

    response = PairResponse.create(
        offer,
        identity,
        identity.card(),
        now=created + 1,
    )
    with pytest.raises(ValueError, match="itself"):
        response.verify(offer, now=created + 2)


def test_cli_pairing_requires_explicit_accept_and_completes_both_peer_books(
    tmp_path,
    capsys,
) -> None:
    a_home = tmp_path / "a"
    b_home = tmp_path / "b"
    offer_path = tmp_path / "a.offer.json"
    response_path = tmp_path / "b.response.json"

    assert main(["--home", str(a_home), "init", "--label", "a", "--port", "48101"]) == 0
    a_init = json.loads(capsys.readouterr().out)
    assert main(["--home", str(b_home), "init", "--label", "b", "--port", "48102"]) == 0
    b_init = json.loads(capsys.readouterr().out)

    assert main(["--home", str(a_home), "pair-offer", "--out", str(offer_path)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["node_id"] == a_init["node_id"]

    assert main(["--home", str(a_home), "peer-list"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["--home", str(b_home), "peer-list"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert (
        main(
            [
                "--home",
                str(b_home),
                "pair-accept",
                str(offer_path),
                "--out",
                str(response_path),
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["accepted"] == a_init["node_id"]

    assert (
        main(
            [
                "--home",
                str(a_home),
                "pair-complete",
                str(offer_path),
                str(response_path),
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["completed"] == b_init["node_id"]

    assert main(["--home", str(a_home), "peer-list"]) == 0
    assert [item["node_id"] for item in json.loads(capsys.readouterr().out)] == [
        b_init["node_id"]
    ]
    assert main(["--home", str(b_home), "peer-list"]) == 0
    assert [item["node_id"] for item in json.loads(capsys.readouterr().out)] == [
        a_init["node_id"]
    ]
