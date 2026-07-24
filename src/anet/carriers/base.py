from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..identity import PeerCard


@dataclass(frozen=True)
class CarrierFrame:
    kind: Literal["packet", "ack"]
    peer_id: str
    packet_id: str
    path: Path
    raw: bytes = b""
    depth: int = 0


@dataclass(frozen=True)
class CarrierScan:
    frames: tuple[CarrierFrame, ...]
    rejected: tuple[Path, ...]


class Carrier(Protocol):
    """Minimal interface implemented by asynchronous Anet carriers."""

    name: str

    def publish_packet(self, peer: PeerCard, raw: bytes, *, depth: int) -> bool: ...

    def publish_ack(self, peer: PeerCard, packet_id: str) -> bool: ...

    def scan(self, peer: PeerCard, *, limit: int = 128) -> CarrierScan: ...

    def consume(self, path: Path) -> None: ...

    def quarantine(self, path: Path) -> Path: ...
