from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


_ZONE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SCOPES = {"host", "lan", "wan"}
_SCOPE_ORDER = {"host": 0, "lan": 1, "legacy": 2, "wan": 3}


@dataclass(frozen=True)
class Locator:
    raw: str
    host: str
    port: int
    scope: str = "legacy"
    zone: str = ""
    priority: int = 100

    @property
    def context(self) -> str:
        if self.scope in {"host", "lan"}:
            return f"{self.scope}:{self.zone}"
        return ""

    def is_usable(self, contexts: tuple[str, ...] | set[str]) -> bool:
        return not self.context or self.context in contexts


def validate_locator_context(value: str) -> str:
    value = str(value).strip()
    try:
        scope, zone = value.split(":", 1)
    except ValueError as exc:
        raise ValueError("locator context must be host:<zone> or lan:<zone>") from exc
    if scope not in {"host", "lan"} or not _ZONE_RE.fullmatch(zone):
        raise ValueError(
            "locator context must use host/lan and an 8-64 character opaque zone"
        )
    return f"{scope}:{zone}"


def parse_locator(address: str) -> Locator:
    raw = str(address).strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"tls", "tcp+tls"}:
        raise ValueError(f"unsupported transport address: {address}")
    if not parsed.hostname:
        raise ValueError(f"address must include host and port: {address}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"address contains an invalid port: {address}") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError(f"address must include host and port: {address}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("transport address must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.fragment:
        raise ValueError("transport address must not contain a path or fragment")

    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in {"scope", "zone", "priority"}:
            raise ValueError(f"unsupported locator parameter: {key}")
        if key in values:
            raise ValueError(f"duplicate locator parameter: {key}")
        values[key] = value

    if not values:
        return Locator(raw=raw, host=parsed.hostname, port=port)

    scope = values.get("scope", "")
    zone = values.get("zone", "")
    if scope not in _SCOPES:
        raise ValueError("scoped locator requires scope=host, lan, or wan")
    if scope in {"host", "lan"}:
        if not _ZONE_RE.fullmatch(zone):
            raise ValueError(
                "host/lan locator requires an 8-64 character opaque zone"
            )
    elif zone:
        raise ValueError("wan locator must not include a zone")
    try:
        priority = int(values.get("priority", "100"))
    except ValueError as exc:
        raise ValueError("locator priority must be an integer") from exc
    if not 0 <= priority <= 10000:
        raise ValueError("locator priority must be between 0 and 10000")
    return Locator(
        raw=raw,
        host=parsed.hostname,
        port=port,
        scope=scope,
        zone=zone,
        priority=priority,
    )


def usable_locators(
    addresses: tuple[str, ...] | list[str], contexts: tuple[str, ...] | set[str]
) -> tuple[Locator, ...]:
    available = [
        locator
        for locator in (parse_locator(address) for address in addresses)
        if locator.is_usable(contexts)
    ]
    return tuple(
        sorted(
            available,
            key=lambda item: (
                item.priority,
                _SCOPE_ORDER[item.scope],
                item.raw,
            ),
        )
    )
