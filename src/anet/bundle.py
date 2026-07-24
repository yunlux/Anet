from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from .encoding import atomic_write, pack, unpack
from .packet import inspect_packet
from .store import PacketStore


BUNDLE_MAGIC = b"ANET-BUNDLE-V1\x00"
LEGACY_BUNDLE_MAGIC = b"AINET-BUNDLE-V1\x00"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024


def create_bundle(store: PacketStore, path: Path, *, destination_id: str = "") -> dict[str, Any]:
    packets = store.export_packets(destination_id=destination_id)
    body = pack(
        {
            "v": 1,
            "created_ms": int(time.time() * 1000),
            "destination_id": str(destination_id),
            "packets": packets,
        }
    )
    raw = BUNDLE_MAGIC + body
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle exceeds size limit")
    digest = hashlib.sha256(raw).hexdigest()
    atomic_write(Path(path), raw)
    return {"path": str(Path(path)), "packets": len(packets), "bytes": len(raw), "sha256": digest}


def import_bundle(store: PacketStore, path: Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("bundle exceeds size limit")
    magic = next(
        (candidate for candidate in (BUNDLE_MAGIC, LEGACY_BUNDLE_MAGIC) if raw.startswith(candidate)),
        None,
    )
    if magic is None:
        raise ValueError("not an Anet bundle")
    value = unpack(raw[len(magic) :])
    if not isinstance(value, dict) or int(value.get("v", 0)) != 1:
        raise ValueError("unsupported Anet bundle")
    packets = value.get("packets")
    if not isinstance(packets, list):
        raise ValueError("bundle packet list is invalid")
    imported = 0
    duplicates = 0
    rejected = 0
    for item in packets:
        try:
            packet = bytes(item)
            inspect_packet(packet)
            if store.add_packet(packet, origin="bundle"):
                imported += 1
            else:
                duplicates += 1
        except Exception:
            rejected += 1
    return {
        "path": str(Path(path)),
        "imported": imported,
        "duplicates": duplicates,
        "rejected": rejected,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
