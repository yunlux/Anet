from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature

from anet.ahub import (
    AHUB_DB_VERSION,
    AhubLimits,
    AhubRequest,
    AhubService,
    AhubStore,
    issue_ahub_request,
    issue_destination_settlement,
)
from anet.control_plane import (
    issue_node_descriptor,
    issue_reachability_record,
)
from anet.encoding import canonical_pack, pack, unpack
from anet.identity import Identity
from anet.packet import inspect_packet, open_packet, seal_packet


NOW = int(time.time() * 1000)
HOUR_MS = 60 * 60 * 1000


def json_body(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def provision(service: AhubService, identity: Identity) -> None:
    service.allow_node(identity.node_id, now=NOW)
    descriptor = issue_node_descriptor(
        identity,
        capabilities=("agent.task",),
        issued_ms=NOW,
        ttl_ms=HOUR_MS,
    )
    service.publish_descriptor(descriptor, now=NOW)


def request(
    identity: Identity,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    now: int = NOW,
    nonce: str | None = None,
) -> AhubRequest:
    return issue_ahub_request(
        identity,
        method=method,
        path=path,
        body=body,
        issued_ms=now,
        nonce=nonce,
    )


def packet(sender: Identity, recipient: Identity, *, body: str = "secret") -> bytes:
    return seal_packet(
        sender,
        recipient.card(),
        kind="agent.message",
        body={"text": body},
        ttl_seconds=60,
    )


def test_rendezvous_requires_allowlist_and_keeps_descriptor_separate(
    tmp_path: Path,
) -> None:
    caller = Identity.generate("caller")
    target = Identity.generate("target")
    outsider = Identity.generate("outsider")

    with AhubService(tmp_path / "ahub") as service:
        provision(service, caller)
        provision(service, target)
        outsider_descriptor = issue_node_descriptor(
            outsider,
            capabilities=(),
            issued_ms=NOW,
            ttl_ms=HOUR_MS,
        )
        with pytest.raises(PermissionError, match="not allowed"):
            service.publish_descriptor(outsider_descriptor, now=NOW)

        target_descriptor = service.control.current_descriptor(
            target.node_id, now=NOW
        )
        assert target_descriptor is not None
        reachability = issue_reachability_record(
            target,
            target_descriptor,
            protocol_versions=("anet/1",),
            candidates=("tls://203.0.113.7:4242",),
            capability_digest=hashlib.sha256(b"agent.task").digest(),
            session_id="a" * 32,
            issued_ms=NOW,
            ttl_ms=60_000,
        )
        service.publish_reachability(reachability, now=NOW)

        auth = request(
            caller, "GET", f"/v1/nodes/{target.node_id}", now=NOW
        )
        descriptor, current = service.lookup(
            auth, b"", target.node_id, now=NOW
        )

        assert descriptor.node_id == target.node_id
        assert "addresses" not in descriptor.to_dict()
        assert current == reachability


def test_request_signature_binds_method_path_body_and_nonce(tmp_path: Path) -> None:
    caller = Identity.generate("caller")
    target = Identity.generate("target")
    path = f"/v1/nodes/{target.node_id}"

    with AhubService(tmp_path / "ahub") as service:
        provision(service, caller)
        provision(service, target)
        signed = request(caller, "GET", path, nonce="n" * 24)

        service.lookup(signed, b"", target.node_id, now=NOW)
        with pytest.raises(PermissionError, match="already used"):
            service.lookup(signed, b"", target.node_id, now=NOW)

        descriptor = service.control.current_descriptor(caller.node_id, now=NOW)
        assert descriptor is not None
        with pytest.raises(ValueError, match="body digest"):
            signed.verify(descriptor, b"tampered", now=NOW)
        with pytest.raises(Exception):
            AhubRequest(
                **{**signed.__dict__, "path": "/v1/mailbox"}
            ).verify(descriptor, b"", now=NOW)


def test_mailbox_custody_claim_settle_preserves_ciphertext(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = packet(sender, recipient)
    info = inspect_packet(raw)

    with AhubService(tmp_path / "ahub") as service:
        provision(service, sender)
        provision(service, recipient)

        upload = request(sender, "POST", "/v1/mailbox", raw)
        receipt = service.submit(upload, raw, now=NOW)
        assert receipt.packet_id == info.packet_id
        assert receipt.stored
        assert service.ahub.mailbox_count(recipient.node_id, now=NOW) == 1

        claim_body = json_body({"lease_ms": 5_000, "limit": 10})
        claim_auth = request(
            recipient, "POST", "/v1/mailbox/claims", claim_body
        )
        claimed = service.claim(
            claim_auth,
            claim_body,
            limit=10,
            lease_ms=5_000,
            now=NOW,
        )
        assert len(claimed) == 1
        assert claimed[0].raw == raw
        assert open_packet(recipient, claimed[0].raw).body == {"text": "secret"}
        assert service.ahub.mailbox_count(recipient.node_id, now=NOW) == 1

        proof = issue_destination_settlement(
            recipient,
            packet_id=claimed[0].packet_id,
            raw=claimed[0].raw,
            uploader_id=claimed[0].uploader_id,
            expires_ms=claimed[0].expires_ms,
            settled_ms=NOW,
        )
        settle_body = json_body(
            {
                "claim_token": claimed[0].claim_token,
                "proof": proof.to_dict(),
            }
        )
        settle_auth = request(
            recipient,
            "POST",
            f"/v1/mailbox/{info.packet_id}/settle",
            settle_body,
        )
        assert service.settle(
            settle_auth,
            settle_body,
            packet_id=info.packet_id,
            claim_token=claimed[0].claim_token,
            proof=proof,
            now=NOW,
        )
        assert service.ahub.mailbox_count(recipient.node_id, now=NOW) == 0


def test_claim_is_destination_only_and_lease_survives_restart(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = packet(sender, recipient)
    root = tmp_path / "ahub"

    service = AhubService(root)
    provision(service, sender)
    provision(service, recipient)
    service.submit(request(sender, "POST", "/v1/mailbox", raw), raw, now=NOW)

    body = json_body({"lease_ms": 5_000, "limit": 1})
    wrong = request(sender, "POST", "/v1/mailbox/claims", body)
    assert service.claim(
        wrong, body, limit=1, lease_ms=5_000, now=NOW
    ) == ()

    first = service.claim(
        request(recipient, "POST", "/v1/mailbox/claims", body),
        body,
        limit=1,
        lease_ms=5_000,
        now=NOW,
    )
    assert len(first) == 1
    service.close()

    later = NOW + 5_001
    with AhubService(root) as restarted:
        reclaimed = restarted.claim(
            request(
                recipient,
                "POST",
                "/v1/mailbox/claims",
                body,
                now=later,
            ),
            body,
            limit=1,
            lease_ms=5_000,
            now=later,
        )
        assert len(reclaimed) == 1
        assert reclaimed[0].raw == raw
        assert reclaimed[0].claim_token != first[0].claim_token


def test_idempotency_conflict_expiry_and_quota_are_transactional(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = packet(sender, recipient)
    info = inspect_packet(raw)
    limits = AhubLimits(
        max_packets_per_destination=1,
        max_packets_per_uploader=1,
    )

    with AhubService(tmp_path / "ahub", limits=limits) as service:
        provision(service, sender)
        provision(service, recipient)
        first = service.submit(
            request(sender, "POST", "/v1/mailbox", raw), raw, now=NOW
        )
        duplicate = service.submit(
            request(sender, "POST", "/v1/mailbox", raw), raw, now=NOW
        )
        assert first.stored
        assert not duplicate.stored

        altered = unpack(raw)
        ciphertext = bytearray(altered["ct"])
        ciphertext[-1] ^= 1
        altered["ct"] = bytes(ciphertext)
        conflicting_raw = pack(altered)
        assert inspect_packet(conflicting_raw).packet_id == info.packet_id
        with pytest.raises(ValueError, match="conflicts"):
            service.submit(
                request(sender, "POST", "/v1/mailbox", conflicting_raw),
                conflicting_raw,
                now=NOW,
            )

        another = packet(sender, recipient, body="another")
        with pytest.raises(OverflowError, match="quota"):
            service.submit(
                request(sender, "POST", "/v1/mailbox", another),
                another,
                now=NOW,
            )
        expired_time = info.expires_ms + 1
        with pytest.raises(ValueError, match="expired"):
            service.submit(
                request(
                    sender,
                    "POST",
                    "/v1/mailbox",
                    raw,
                    now=expired_time,
                ),
                raw,
                now=expired_time,
            )


def test_concurrent_quota_check_has_one_durable_winner(tmp_path: Path) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    root = tmp_path / "ahub"
    limits = AhubLimits(
        max_packets_per_destination=1,
        max_packets_per_uploader=1,
    )
    setup_service = AhubService(root, limits=limits)
    provision(setup_service, sender)
    provision(setup_service, recipient)
    setup_service.close()
    raws = (packet(sender, recipient, body="one"), packet(sender, recipient, body="two"))
    requests = tuple(
        request(sender, "POST", "/v1/mailbox", raw) for raw in raws
    )

    def submit(index: int) -> str:
        with AhubService(root, limits=limits) as service:
            try:
                service.submit(requests[index], raws[index], now=NOW)
            except OverflowError:
                return "quota"
        return "stored"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(submit, (0, 1)))
    assert sorted(results) == ["quota", "stored"]
    with AhubService(root, limits=limits) as service:
        assert service.ahub.mailbox_count(recipient.node_id, now=NOW) == 1


def test_restart_preserves_nonce_replay_fence(tmp_path: Path) -> None:
    caller = Identity.generate("caller")
    target = Identity.generate("target")
    root = tmp_path / "ahub"
    signed = request(
        caller,
        "GET",
        f"/v1/nodes/{target.node_id}",
        nonce="r" * 24,
    )

    service = AhubService(root)
    provision(service, caller)
    provision(service, target)
    service.lookup(signed, b"", target.node_id, now=NOW)
    service.close()

    with AhubService(root) as restarted:
        with pytest.raises(PermissionError, match="already used"):
            restarted.lookup(signed, b"", target.node_id, now=NOW)


def test_ahub_databases_contain_no_private_key_columns(tmp_path: Path) -> None:
    node = Identity.generate("node")
    root = tmp_path / "ahub"
    with AhubService(root) as service:
        provision(service, node)

    for database in (root / "ahub.sqlite3", root / "control.sqlite3"):
        connection = sqlite3.connect(database)
        try:
            schemas = "\n".join(
                str(row[0])
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                )
            ).lower()
        finally:
            connection.close()
        assert "private" not in schemas
        assert "password" not in schemas


def test_disallow_is_durable_reversible_and_retains_pending_ciphertext(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = packet(sender, recipient)
    root = tmp_path / "ahub"

    with AhubService(root) as service:
        provision(service, sender)
        provision(service, recipient)
        service.submit(
            request(sender, "POST", "/v1/mailbox", raw), raw, now=NOW
        )
        assert service.disallow_node(recipient.node_id, now=NOW + 1)
        assert not service.ahub.is_allowed(recipient.node_id)
        body = json_body({"lease_ms": 5_000, "limit": 1})
        with pytest.raises(PermissionError, match="not allowed"):
            service.claim(
                request(
                    recipient,
                    "POST",
                    "/v1/mailbox/claims",
                    body,
                    now=NOW + 1,
                ),
                body,
                limit=1,
                lease_ms=5_000,
                now=NOW + 1,
            )
        assert service.ahub.mailbox_count(recipient.node_id, now=NOW + 1) == 1

    with AhubService(root) as restarted:
        nodes = restarted.ahub.allowed_nodes(include_disabled=True)
        recipient_row = next(
            item for item in nodes if item["node_id"] == recipient.node_id
        )
        assert recipient_row["enabled"] is False
        assert restarted.allow_node(recipient.node_id, now=NOW + 2)
        body = json_body({"lease_ms": 5_000, "limit": 1})
        claimed = restarted.claim(
            request(
                recipient,
                "POST",
                "/v1/mailbox/claims",
                body,
                now=NOW + 2,
            ),
            body,
            limit=1,
            lease_ms=5_000,
            now=NOW + 2,
        )
        assert len(claimed) == 1
        assert claimed[0].raw == raw


def test_status_purge_and_expired_reachability_are_bounded(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = packet(sender, recipient)
    root = tmp_path / "ahub"

    with AhubService(root) as service:
        provision(service, sender)
        provision(service, recipient)
        descriptor = service.control.current_descriptor(recipient.node_id, now=NOW)
        assert descriptor is not None
        reachable = issue_reachability_record(
            recipient,
            descriptor,
            protocol_versions=("anet/1",),
            capability_digest=hashlib.sha256(b"agent.task").digest(),
            session_id="b" * 32,
            issued_ms=NOW,
            ttl_ms=60_000,
        )
        service.publish_reachability(reachable, now=NOW)
        service.submit(
            request(sender, "POST", "/v1/mailbox", raw), raw, now=NOW
        )
        initial = service.status(now=NOW)
        assert initial["enabled_nodes"] == 2
        assert initial["mailbox_packets"] == 1
        assert initial["live_reachability"] == 1
        assert service.health()

        later = NOW + 11 * 60 * 1000
        assert service.control.current_reachability(
            recipient.node_id, now=later
        ) is None
        purged = service.ahub.purge(now=later)
        assert purged["expired_packets"] == 1
        assert purged["old_nonces"] == 1
        final = service.status(now=later)
        assert final["mailbox_packets"] == 0
        assert final["live_reachability"] == 0
        assert final["expired_reachability"] == 1


def test_ahub_v1_database_migrates_allowlist_without_losing_nodes(
    tmp_path: Path,
) -> None:
    identity = Identity.generate("legacy")
    database = tmp_path / "ahub.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE ahub_allowlist (
            node_id TEXT PRIMARY KEY,
            added_ms INTEGER NOT NULL
        );
        CREATE TABLE ahub_nonces (
            node_id TEXT NOT NULL,
            nonce TEXT NOT NULL,
            issued_ms INTEGER NOT NULL,
            PRIMARY KEY(node_id, nonce),
            FOREIGN KEY(node_id) REFERENCES ahub_allowlist(node_id)
        );
        CREATE TABLE ahub_mailbox (
            packet_id TEXT PRIMARY KEY,
            destination_id TEXT NOT NULL,
            uploader_id TEXT NOT NULL,
            raw BLOB NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_ms INTEGER NOT NULL,
            expires_ms INTEGER NOT NULL,
            qos TEXT NOT NULL,
            depth INTEGER NOT NULL,
            claim_token_hash BLOB,
            claim_until_ms INTEGER
        );
        PRAGMA user_version = 1;
        """
    )
    connection.execute(
        "INSERT INTO ahub_allowlist(node_id, added_ms) VALUES (?, ?)",
        (identity.node_id, NOW),
    )
    connection.commit()
    connection.close()

    with AhubStore(database) as migrated:
        assert migrated.is_allowed(identity.node_id)
        assert migrated.status()["database_version"] == AHUB_DB_VERSION
        row = migrated.allowed_nodes()[0]
        assert row["node_id"] == identity.node_id
        assert row["enabled"] is True


def test_ahub_cannot_accept_forged_destination_settlement(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    attacker = Identity.generate("attacker")
    raw = packet(sender, recipient)
    with AhubService(tmp_path / "ahub") as service:
        provision(service, sender)
        provision(service, recipient)
        service.submit(
            request(sender, "POST", "/v1/mailbox", raw), raw, now=NOW
        )
        claim_body = json_body({"lease_ms": 5_000, "limit": 1})
        claim = service.claim(
            request(
                recipient, "POST", "/v1/mailbox/claims", claim_body
            ),
            claim_body,
            limit=1,
            lease_ms=5_000,
            now=NOW,
        )[0]
        valid = issue_destination_settlement(
            recipient,
            packet_id=claim.packet_id,
            raw=claim.raw,
            uploader_id=claim.uploader_id,
            expires_ms=claim.expires_ms,
            settled_ms=NOW,
        )
        forged = replace(
            valid,
            signature=attacker.sign(canonical_pack(valid.signing_fields())),
        )
        settle_body = json_body(
            {
                "claim_token": claim.claim_token,
                "proof": forged.to_dict(),
            }
        )
        with pytest.raises(InvalidSignature):
            service.settle(
                request(
                    recipient,
                    "POST",
                    f"/v1/mailbox/{claim.packet_id}/settle",
                    settle_body,
                ),
                settle_body,
                packet_id=claim.packet_id,
                claim_token=claim.claim_token,
                proof=forged,
                now=NOW,
            )
        assert service.ahub.mailbox_count(recipient.node_id, now=NOW) == 1
        assert service.status(now=NOW)["retained_settlements"] == 0


def test_settlement_acknowledgements_prevent_batch_starvation(
    tmp_path: Path,
) -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    with AhubService(tmp_path / "ahub") as service:
        provision(service, sender)
        provision(service, recipient)
        for index in range(11):
            raw = packet(sender, recipient, body=f"message-{index}")
            service.submit(
                request(sender, "POST", "/v1/mailbox", raw),
                raw,
                now=NOW,
            )
        claim_body = json_body({"lease_ms": 5_000, "limit": 11})
        claims = service.claim(
            request(
                recipient, "POST", "/v1/mailbox/claims", claim_body
            ),
            claim_body,
            limit=11,
            lease_ms=5_000,
            now=NOW,
        )
        assert len(claims) == 11
        for claim in claims:
            proof = issue_destination_settlement(
                recipient,
                packet_id=claim.packet_id,
                raw=claim.raw,
                uploader_id=claim.uploader_id,
                expires_ms=claim.expires_ms,
                settled_ms=NOW,
            )
            body = json_body(
                {"claim_token": claim.claim_token, "proof": proof.to_dict()}
            )
            assert service.settle(
                request(
                    recipient,
                    "POST",
                    f"/v1/mailbox/{claim.packet_id}/settle",
                    body,
                ),
                body,
                packet_id=claim.packet_id,
                claim_token=claim.claim_token,
                proof=proof,
                now=NOW,
            )

        settlements_body = json_body(
            {"destination_id": recipient.node_id, "limit": 10}
        )
        first = service.settlements(
            request(
                sender,
                "POST",
                "/v1/mailbox/settlements",
                settlements_body,
            ),
            settlements_body,
            limit=10,
            destination_id=recipient.node_id,
            now=NOW,
        )
        assert len(first) == 10
        for proof in first:
            path = f"/v1/mailbox/settlements/{proof.packet_id}/ack"
            assert service.acknowledge_settlement(
                request(sender, "POST", path),
                b"",
                packet_id=proof.packet_id,
                now=NOW,
            )
        remaining = service.settlements(
            request(
                sender,
                "POST",
                "/v1/mailbox/settlements",
                settlements_body,
            ),
            settlements_body,
            limit=10,
            destination_id=recipient.node_id,
            now=NOW,
        )
        assert len(remaining) == 1
        assert remaining[0].packet_id not in {
            item.packet_id for item in first
        }
