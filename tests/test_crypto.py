from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from anet.encoding import pack, unpack
from anet.identity import Identity, PeerCard
from anet.packet import inspect_packet, open_packet, seal_packet
from anet.prekeys import derive_prekey_id


def test_tls_certificate_must_match_its_private_key(tmp_path) -> None:
    identity = Identity.generate("node")
    identity.ensure_tls_material(tmp_path)
    replacement = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (tmp_path / "tls-key.pem").write_bytes(replacement)

    with pytest.raises(ValueError, match="do not match"):
        identity.ensure_tls_material(tmp_path)


def test_peer_card_is_self_authenticating() -> None:
    identity = Identity.generate("node_a")
    card = identity.card(addresses=["tls://127.0.0.1:4242"], capabilities=["agent-message-v0"])
    card.verify()
    assert card.node_id == identity.node_id

    value = card.to_dict()
    value["label"] = "forged"
    with pytest.raises(InvalidSignature):
        PeerCard.from_dict(value)


def test_packet_round_trip_hides_sender_and_exact_length() -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    raw = seal_packet(
        sender,
        recipient.card(),
        kind="agent.ir",
        body={"performative": "PROPOSE", "confidence": 0.84},
        causal=["0" * 32],
        padding_min=1024,
    )
    outer = unpack(raw)
    assert sender.node_id.encode() not in raw
    assert len(outer["ct"]) == 1024 + 16
    info = inspect_packet(raw)
    assert info.destination_id == recipient.node_id

    message = open_packet(recipient, raw)
    assert message.sender_id == sender.node_id
    assert message.kind == "agent.ir"
    assert message.body["performative"] == "PROPOSE"
    assert message.causal == ("0" * 32,)


def test_packet_tampering_and_wrong_recipient_are_rejected() -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    other = Identity.generate("other")
    raw = seal_packet(sender, recipient.card(), kind="message", body="secret")

    with pytest.raises(ValueError, match="another node"):
        open_packet(other, raw)

    outer = unpack(raw)
    ciphertext = bytearray(outer["ct"])
    ciphertext[-1] ^= 1
    outer["ct"] = bytes(ciphertext)
    with pytest.raises(InvalidTag):
        open_packet(recipient, pack(outer))

    outer = unpack(raw)
    outer["dst"] = other.node_id
    with pytest.raises((InvalidTag, ValueError)):
        open_packet(other, pack(outer))


def test_qos_is_authenticated_and_legacy_v1_remains_readable(monkeypatch) -> None:
    import anet.packet as packet_module

    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    current = seal_packet(sender, recipient.card(), kind="message", body="fast", qos="control")
    assert inspect_packet(current).qos == "control"
    assert open_packet(recipient, current).qos == "control"

    monkeypatch.setattr(packet_module, "PACKET_VERSION", 1)
    legacy_with_extra = seal_packet(sender, recipient.card(), kind="message", body="legacy")
    legacy_outer = unpack(legacy_with_extra)
    legacy_outer.pop("qos")
    legacy = pack(legacy_outer)
    assert inspect_packet(legacy).qos == "normal"
    assert open_packet(recipient, legacy).body == "legacy"


def test_v3_one_time_prekey_round_trip_and_binding() -> None:
    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    prekey = X25519PrivateKey.generate()
    prekey_private = prekey.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    prekey_public = prekey.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    prekey_id = derive_prekey_id(prekey_public)
    raw = seal_packet(
        sender,
        recipient.card(),
        kind="message",
        body="forward-secret",
        recipient_prekey_public=prekey_public,
        recipient_prekey_id=prekey_id,
    )
    info = inspect_packet(raw)
    assert info.key_mode == "opk"
    assert info.prekey_id == prekey_id
    with pytest.raises(ValueError, match="requires a one-time prekey"):
        open_packet(recipient, raw)
    opened = open_packet(
        recipient, raw, recipient_prekey_private=prekey_private
    )
    assert opened.body == "forward-secret"
    assert opened.key_mode == "opk"
    assert opened.prekey_id == prekey_id

    wrong = X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="does not match"):
        open_packet(recipient, raw, recipient_prekey_private=wrong)


def test_legacy_v2_packet_key_derivation_remains_readable(monkeypatch) -> None:
    import anet.packet as packet_module

    sender = Identity.generate("sender")
    recipient = Identity.generate("recipient")
    monkeypatch.setattr(packet_module, "PACKET_VERSION", 2)
    raw = seal_packet(sender, recipient.card(), kind="message", body="v2")
    assert inspect_packet(raw).key_mode == "static"
    assert open_packet(recipient, raw).body == "v2"
