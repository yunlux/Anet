from __future__ import annotations

import json

import pytest
from cryptography.exceptions import InvalidSignature

from anet.cli import main
from anet.config import NodeConfig
from anet.identity import Identity
from anet.relationship_claims import (
    MutualRelationshipClaim,
    RelationshipClaimBook,
    RelationshipProposal,
)
from anet.relations import RelationshipBook


def test_mutual_relationship_claim_is_bound_to_both_actors_and_survives_expiry(
    tmp_path,
) -> None:
    proposer = Identity.generate("proposer")
    accepter = Identity.generate("accepter")
    other = Identity.generate("other")
    created = 1_800_000_000_000
    proposal = RelationshipProposal.create(
        proposer,
        proposer.card(),
        peer_actor_id=accepter.node_id,
        circle="friend",
        labels=("research-partner",),
        ttl_seconds=60,
        now=created,
    )
    with pytest.raises(ValueError, match="proposed relationship peer"):
        MutualRelationshipClaim.create(
            proposal,
            other,
            other.card(),
            now=created + 10_000,
        )

    claim = MutualRelationshipClaim.create(
        proposal,
        accepter,
        accepter.card(),
        now=created + 30_000,
    )
    claim.verify(now=created + 120_000)
    assert claim.peer_card_for(proposer.node_id).node_id == accepter.node_id
    assert claim.peer_card_for(accepter.node_id).node_id == proposer.node_id
    assert "subj_" not in json.dumps(claim.to_dict())

    claim_path = tmp_path / "claim.json"
    claim.save(claim_path)
    assert MutualRelationshipClaim.load(
        claim_path,
        now=created + 120_000,
    ).claim_id == claim.claim_id


def test_relationship_claim_rejects_tampering_and_late_acceptance() -> None:
    proposer = Identity.generate("proposer")
    accepter = Identity.generate("accepter")
    created = 1_800_000_000_000
    proposal = RelationshipProposal.create(
        proposer,
        proposer.card(),
        peer_actor_id=accepter.node_id,
        circle="collab",
        ttl_seconds=60,
        now=created,
    )
    value = proposal.to_dict()
    value["circle"] = "friend"
    tampered = RelationshipProposal.from_dict(value)
    with pytest.raises(InvalidSignature):
        tampered.verify(now=created + 10_000)

    with pytest.raises(ValueError, match="expired"):
        MutualRelationshipClaim.create(
            proposal,
            accepter,
            accepter.card(),
            now=created + 61_000,
        )


def test_claim_book_and_projection_are_idempotent_and_do_not_downgrade(
    tmp_path,
) -> None:
    observer = Identity.generate("observer")
    peer = Identity.generate("peer")
    proposal = RelationshipProposal.create(
        observer,
        observer.card(),
        peer_actor_id=peer.node_id,
        circle="collab",
        now=1_700_000_000_000,
    )
    claim = MutualRelationshipClaim.create(
        proposal,
        peer,
        peer.card(),
        now=1_700_000_001_000,
    )
    claims = RelationshipClaimBook(tmp_path / "claims.json")
    assert claims.add(claim) is True
    assert claims.add(claim) is False
    assert len(RelationshipClaimBook(tmp_path / "claims.json").all()) == 1

    relationships = RelationshipBook(
        tmp_path / "relationships.json",
        own_actor_id=observer.node_id,
    )
    subject = relationships.observe_actor(
        peer.card(),
        evidence_ref="packet:peer",
        now=1_800_000_002_000,
    )
    relationships.set_circle(
        subject.subject_ref,
        "close",
        confidence=70,
        evidence_ref="relationship:local-close",
        now=1_800_000_003_000,
    )
    projected = relationships.confirm_mutual_relationship(
        peer.card(),
        claim.circle,
        labels=claim.labels,
        evidence_ref=f"mutual:{claim.claim_id}",
        now=1_800_000_004_000,
    )
    assert projected.circle == "close"
    assert "relationship:mutual:collab" in projected.relationship_labels
    before = relationships.snapshot()
    relationships.confirm_mutual_relationship(
        peer.card(),
        claim.circle,
        labels=claim.labels,
        evidence_ref=f"mutual:{claim.claim_id}",
        now=1_800_000_004_000,
    )
    assert relationships.snapshot() == before


def test_relationship_claim_cli_projects_both_views_without_peer_trust(
    tmp_path,
    capsys,
) -> None:
    a_home = tmp_path / "a"
    b_home = tmp_path / "b"
    proposal_path = tmp_path / "proposal.json"
    claim_path = tmp_path / "claim.json"
    assert main(["--home", str(a_home), "init", "--label", "a", "--port", "48401"]) == 0
    a_init = json.loads(capsys.readouterr().out)
    assert main(["--home", str(b_home), "init", "--label", "b", "--port", "48402"]) == 0
    b_init = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "--home",
                str(a_home),
                "relation-propose",
                b_init["node_id"],
                "friend",
                "--label",
                "research-partner",
                "--out",
                str(proposal_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--home",
                str(b_home),
                "relation-accept",
                str(proposal_path),
                "--out",
                str(claim_path),
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["peer_actor_id"] == a_init["node_id"]
    assert accepted["circle"] == "friend"
    assert accepted["trust_changed"] is False
    assert accepted["capabilities_granted"] == []

    assert main(["--home", str(a_home), "relation-import", str(claim_path)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["peer_actor_id"] == b_init["node_id"]
    assert imported["circle"] == "friend"
    assert main(["--home", str(a_home), "relation-claim-list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["claim_id"] == imported["claim_id"]
    assert listed[0]["circle"] == "friend"

    for home, own_id, peer_id in (
        (a_home, a_init["node_id"], b_init["node_id"]),
        (b_home, b_init["node_id"], a_init["node_id"]),
    ):
        config = NodeConfig.load(home)
        model = RelationshipBook(
            config.relationships_path,
            own_actor_id=own_id,
        )
        assert model.get(peer_id).circle == "friend"
        assert len(RelationshipClaimBook(config.relationship_claims_path).all()) == 1
        peers_value = json.loads(config.peers_path.read_text(encoding="utf-8"))
        assert peers_value["peers"] == []

    config = NodeConfig.load(a_home)
    before = RelationshipBook(
        config.relationships_path,
        own_actor_id=a_init["node_id"],
    ).snapshot()
    assert main(["--home", str(a_home), "relation-import", str(claim_path)]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["stored"] is False
    after = RelationshipBook(
        config.relationships_path,
        own_actor_id=a_init["node_id"],
    ).snapshot()
    assert after == before
