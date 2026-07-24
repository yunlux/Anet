from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .encoding import atomic_json
from .identity import PeerCard


class PeerBook:
    def __init__(self, path: Path, *, own_node_id: str = "") -> None:
        self.path = Path(path)
        self.revocations_path = self.path.with_name("revocations.json")
        self.own_node_id = own_node_id
        self._peers: dict[str, PeerCard] = {}
        self._revocations: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        revocations: dict[str, dict[str, Any]] = {}
        if self.revocations_path.exists():
            revoked_value = json.loads(self.revocations_path.read_text(encoding="utf-8"))
            if int(revoked_value.get("version", 0)) != 1:
                raise ValueError("unsupported peer revocation file")
            for item in revoked_value.get("revocations", []):
                node_id = str(item.get("node_id", ""))
                if not node_id or node_id == self.own_node_id:
                    raise ValueError("invalid peer revocation entry")
                revocations[node_id] = {
                    "node_id": node_id,
                    "revoked_ms": int(item.get("revoked_ms", 0)),
                    "reason": str(item.get("reason", ""))[:500],
                    "key_fingerprint": str(item.get("key_fingerprint", "")),
                }
        self._revocations = revocations
        if not self.path.exists():
            self._peers = {}
            return
        value = json.loads(self.path.read_text(encoding="utf-8"))
        peers: dict[str, PeerCard] = {}
        for item in value.get("peers", []):
            card = PeerCard.from_dict(item)
            if card.node_id != self.own_node_id and card.node_id not in revocations:
                peers[card.node_id] = card
        self._peers = peers

    def save(self) -> None:
        atomic_json(
            self.path,
            {"version": 1, "peers": [self._peers[key].to_dict() for key in sorted(self._peers)]},
            private=True,
        )

    def add(self, card: PeerCard) -> None:
        card.verify()
        if card.node_id == self.own_node_id:
            raise ValueError("cannot add the local node as a peer")
        if card.node_id in self._revocations:
            raise ValueError("peer identity is locally revoked")
        existing = self._peers.get(card.node_id)
        if existing and (existing.sign_public != card.sign_public or existing.box_public != card.box_public):
            raise ValueError("peer identity key mismatch")
        self._peers[card.node_id] = card
        self.save()

    @staticmethod
    def _key_fingerprint(card: PeerCard) -> str:
        return hashlib.sha256(card.sign_public + card.box_public).hexdigest()

    def _save_revocations(self) -> None:
        atomic_json(
            self.revocations_path,
            {
                "version": 1,
                "revocations": [
                    self._revocations[key] for key in sorted(self._revocations)
                ],
            },
            private=True,
        )

    def revoke(self, node_id: str, *, reason: str = "") -> dict[str, Any]:
        node_id = str(node_id).strip()
        existing = self._revocations.get(node_id)
        if existing is not None:
            return dict(existing)
        card = self._peers.get(node_id)
        if card is None:
            raise KeyError(f"unknown peer: {node_id}")
        record = {
            "node_id": node_id,
            "revoked_ms": int(time.time() * 1000),
            "reason": str(reason).strip()[:500],
            "key_fingerprint": self._key_fingerprint(card),
        }
        # Persist the deny record before removing the positive trust record.
        # A crash between the two writes therefore fails closed on reload.
        self._revocations[node_id] = record
        self._save_revocations()
        self._peers.pop(node_id, None)
        self.save()
        return dict(record)

    def revocations(self) -> list[dict[str, Any]]:
        return [dict(self._revocations[key]) for key in sorted(self._revocations)]

    def get(self, node_id: str) -> PeerCard | None:
        return self._peers.get(str(node_id))

    def require(self, node_id: str) -> PeerCard:
        # A separate administrative process may have written a revocation while
        # this node is running. Reload at trust boundaries so new handshakes and
        # outbound work observe the deny record immediately.
        self.reload()
        card = self.get(node_id)
        if card is None:
            raise KeyError(f"unknown peer: {node_id}")
        return card

    def is_trusted(self, node_id: str, sign_public: bytes, box_public: bytes) -> bool:
        self.reload()
        card = self.get(node_id)
        return bool(card and card.sign_public == sign_public and card.box_public == box_public)

    def all(self) -> list[PeerCard]:
        return [self._peers[key] for key in sorted(self._peers)]
