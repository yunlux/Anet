from __future__ import annotations

import json
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidSignature

from anet.cli import main
from anet.friendship import (
    FriendAcceptance,
    FriendInvite,
    decode_friend_code,
    encode_friend_code,
    read_friend_code,
    write_friend_code,
)
from anet.identity import Identity
from anet.relations import RelationshipBook


def test_friend_codes_are_signed_compact_and_challenge_bound() -> None:
    created = 1_800_000_000_000
    a = Identity.generate("a")
    b = Identity.generate("b")
    invite = FriendInvite.create(
        a,
        a.card(addresses=("tls://10.0.0.1:4242",)),
        ttl_seconds=600,
        now=created,
    )
    invite_text = encode_friend_code(invite)
    assert len(invite_text.encode("utf-8")) < 2953
    decoded_invite = decode_friend_code(invite_text, now=created + 1)
    assert decoded_invite == invite

    acceptance = FriendAcceptance.create(
        invite,
        b,
        b.card(addresses=("tls://10.0.0.2:4242",)),
        now=created + 2,
    )
    acceptance_text = encode_friend_code(acceptance)
    assert len(acceptance_text.encode("utf-8")) < 2953
    decoded_acceptance = decode_friend_code(
        acceptance_text,
        now=created + 3,
    )
    assert decoded_acceptance == acceptance

    with pytest.raises(InvalidSignature):
        replace(invite, signature=b"\x00" * 64).verify(now=created + 3)
    with pytest.raises(ValueError, match="bound"):
        replace(
            acceptance,
            invite_digest=b"\x00" * 32,
        ).verify(now=created + 3)


def test_friend_code_text_adapter_round_trips(tmp_path) -> None:
    identity = Identity.generate("a")
    invite = FriendInvite.create(identity, identity.card())
    payload = encode_friend_code(invite)
    path = tmp_path / "friend.anetqr"
    write_friend_code(payload, path)
    assert read_friend_code(path) == payload
    assert read_friend_code(payload) == payload


def test_friend_code_png_round_trips_through_qr_scanner(tmp_path) -> None:
    identity = Identity.generate("a")
    invite = FriendInvite.create(identity, identity.card())
    payload = encode_friend_code(invite)
    path = tmp_path / "friend.png"
    write_friend_code(payload, path)
    assert path.stat().st_size > 1000
    assert read_friend_code(path) == payload


def test_qr_friend_cli_pins_both_peers_and_creates_local_circle_records(
    tmp_path,
    capsys,
) -> None:
    a_home = tmp_path / "a"
    b_home = tmp_path / "b"
    invite_path = tmp_path / "a-friend.png"
    response_path = tmp_path / "b-friend.png"

    assert main(["--home", str(a_home), "init", "--label", "a", "--port", "48201"]) == 0
    a_init = json.loads(capsys.readouterr().out)
    assert main(["--home", str(b_home), "init", "--label", "b", "--port", "48202"]) == 0
    b_init = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "--home",
                str(a_home),
                "friend-qr",
                "--out",
                str(invite_path),
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["relationship"] == "friend"

    assert (
        main(
            [
                "--home",
                str(b_home),
                "friend-scan",
                str(invite_path),
                "--out",
                str(response_path),
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["accepted"] == a_init["node_id"]
    assert accepted["circle"] == "friend"

    assert (
        main(
            [
                "--home",
                str(a_home),
                "friend-scan",
                str(response_path),
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["completed"] == b_init["node_id"]
    assert completed["circle"] == "friend"

    for home, expected_actor in (
        (a_home, b_init["node_id"]),
        (b_home, a_init["node_id"]),
    ):
        assert main(["--home", str(home), "relation-list"]) == 0
        relations = json.loads(capsys.readouterr().out)
        assert len(relations) == 1
        assert relations[0]["actor_id"] == expected_actor
        assert relations[0]["circle"] == "friend"
        assert relations[0]["state"] == "active"
        assert relations[0]["relationship_confidence"] == 100
        assert relations[0]["subject_confidence"] == 50
        assert relations[0]["subject_ref"].startswith("subj_")

        book = RelationshipBook(
            home / "relationships.json",
            own_actor_id=(a_init["node_id"] if home == a_home else b_init["node_id"]),
        )
        assert book.get(expected_actor) is not None

    assert (
        main(
            [
                "--home",
                str(a_home),
                "peer-revoke",
                b_init["node_id"],
                "--confirm",
                b_init["node_id"],
                "--reason",
                "test revocation",
            ]
        )
        == 0
    )
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["relationship"]["state"] == "active"
    assert revoked["relationship"]["circle"] == "friend"

    assert main(["--home", str(a_home), "relation-list", "--model"]) == 0
    model = json.loads(capsys.readouterr().out)
    assert model["version"] == 4
    assert model["actors"][0]["state"] == "revoked"
    assert model["relationships"][0]["circle"] == "friend"
    assert model["events"][-1]["event_type"] == "actor.revoked"
