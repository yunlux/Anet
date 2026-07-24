from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .encoding import atomic_json
from .identity import Identity
from .locator import parse_locator, validate_locator_context


CONFIG_VERSION = 1
DEFAULT_CAPABILITIES = (
    "agent-message-v0",
    "store-forward-v0",
    "bundle-v0",
    "consumer-groups-v1",
    "directory-carrier-v1",
    "directory-carrier-v2",
    "link-health-v1",
    "one-time-prekeys-v1",
    "one-time-prekeys-v2",
    "qos-v1",
    "webdav-carrier-v1",
    "webdav-carrier-v2",
    "ahub-carrier-v1",
)
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class DirectProxyConfig:
    url: str
    allow_remote: bool = False
    username_env: str = ""
    password_env: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "allow_remote": self.allow_remote,
            "username_env": self.username_env,
            "password_env": self.password_env,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DirectProxyConfig":
        url = str(value.get("url", "")).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"socks5", "socks5h"}:
            raise ValueError("direct proxy URL scheme must be socks5 or socks5h")
        if not parsed.hostname:
            raise ValueError("direct proxy URL must include a host and port")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("direct proxy URL must not contain credentials")
        if parsed.path or "?" in url or "#" in url:
            raise ValueError(
                "direct proxy URL must not contain a path, query, or fragment"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("direct proxy URL contains an invalid port") from exc
        if port is None or not 1 <= port <= 65535:
            raise ValueError("direct proxy URL must include a valid port")
        allow_remote = value.get("allow_remote", False)
        if not isinstance(allow_remote, bool):
            raise ValueError("direct proxy allow_remote must be a boolean")
        host = parsed.hostname
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if not loopback and not allow_remote:
            raise ValueError("remote direct proxy host requires allow_remote")
        username_env = str(value.get("username_env", "")).strip()
        password_env = str(value.get("password_env", "")).strip()
        if bool(username_env) != bool(password_env):
            raise ValueError(
                "proxy authentication requires both username and password env names"
            )
        if any(
            name and not _ENV_RE.fullmatch(name)
            for name in (username_env, password_env)
        ):
            raise ValueError("invalid proxy credential environment variable name")
        return cls(
            url=url,
            allow_remote=allow_remote,
            username_env=username_env,
            password_env=password_env,
        )


@dataclass(frozen=True)
class StdioDialerConfig:
    """An external byte-stream adapter launched without a command shell."""

    executable: Path
    args: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    startup_timeout: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": str(self.executable),
            "args": list(self.args),
            "env": list(self.env),
            "startup_timeout": self.startup_timeout,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StdioDialerConfig":
        executable_raw = str(value.get("executable", "")).strip()
        if not executable_raw:
            raise ValueError("stdio dialer executable is required")
        executable = Path(executable_raw).expanduser()
        if not executable.is_absolute():
            raise ValueError("stdio dialer executable must be an absolute path")
        executable = executable.resolve(strict=False)

        args_raw = value.get("args", [])
        if not isinstance(args_raw, (list, tuple)):
            raise ValueError("stdio dialer args must be a list")
        if len(args_raw) > 32:
            raise ValueError("stdio dialer accepts at most 32 arguments")
        args: list[str] = []
        for item in args_raw:
            argument = str(item)
            if not argument or len(argument) > 4096:
                raise ValueError("stdio dialer arguments must contain 1-4096 characters")
            if "\x00" in argument:
                raise ValueError("stdio dialer arguments must not contain NUL bytes")
            if "\r" in argument or "\n" in argument:
                raise ValueError("stdio dialer arguments must not contain a newline")
            args.append(argument)

        env_raw = value.get("env", [])
        if not isinstance(env_raw, (list, tuple)):
            raise ValueError("stdio dialer env must be a list")
        if len(env_raw) > 32:
            raise ValueError("stdio dialer accepts at most 32 environment variables")
        env = tuple(str(item).strip() for item in env_raw)
        if any(not name or not _ENV_RE.fullmatch(name) for name in env):
            raise ValueError("invalid stdio dialer environment variable name")
        if len(env) != len(set(env)):
            raise ValueError("stdio dialer environment variable names must be unique")
        reserved = {"ANET_TARGET_HOST", "ANET_TARGET_PORT"}
        if reserved.intersection(env):
            raise ValueError("stdio dialer target environment variables are reserved")

        startup_timeout = float(value.get("startup_timeout", 5.0))
        if not 0.5 <= startup_timeout <= 60.0:
            raise ValueError("stdio dialer startup timeout must be between 0.5 and 60 seconds")
        return cls(
            executable=executable,
            args=tuple(args),
            env=env,
            startup_timeout=startup_timeout,
        )


@dataclass(frozen=True)
class DirectDialerConfig:
    name: str
    priority: int = 100
    enabled: bool = True
    proxy: DirectProxyConfig | None = None
    stdio: StdioDialerConfig | None = None

    def __post_init__(self) -> None:
        if self.proxy is not None and self.stdio is not None:
            raise ValueError("direct dialer cannot combine proxy and stdio adapters")

    @property
    def kind(self) -> str:
        if self.stdio is not None:
            return "stdio"
        if self.proxy is not None:
            return urlsplit(self.proxy.url).scheme
        return "raw"

    @property
    def path_prefix(self) -> str:
        return f"direct:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "type": self.kind,
            "priority": self.priority,
            "enabled": self.enabled,
        }
        if self.proxy is not None:
            value.update(self.proxy.to_dict())
        if self.stdio is not None:
            value.update(self.stdio.to_dict())
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DirectDialerConfig":
        name = str(value.get("name", "")).strip()
        if (
            not name
            or len(name) > 64
            or not all(ch.isalnum() or ch in "-_" for ch in name)
        ):
            raise ValueError("invalid direct dialer name")
        kind = str(value.get("type", "raw")).strip().lower()
        if kind not in {"raw", "socks5", "socks5h", "stdio"}:
            raise ValueError(
                "direct dialer type must be raw, socks5, socks5h, or stdio"
            )
        priority = int(value.get("priority", 100))
        if not 0 <= priority <= 10000:
            raise ValueError("direct dialer priority must be between 0 and 10000")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("direct dialer enabled must be a boolean")
        proxy = None
        stdio = None
        if kind in {"socks5", "socks5h"}:
            proxy = DirectProxyConfig.from_dict(value)
            if urlsplit(proxy.url).scheme != kind:
                raise ValueError("direct dialer type must match proxy URL scheme")
        elif kind == "stdio":
            if value.get("url"):
                raise ValueError("stdio direct dialer must not contain a proxy URL")
            stdio = StdioDialerConfig.from_dict(value)
        elif value.get("url"):
            raise ValueError("raw direct dialer must not contain a proxy URL")
        return cls(
            name=name,
            priority=priority,
            enabled=enabled,
            proxy=proxy,
            stdio=stdio,
        )


@dataclass(frozen=True)
class DirectoryCarrierConfig:
    name: str
    path: Path
    peers: tuple[str, ...] = ()
    mode: str = "fallback"
    interval: float = 2.0
    jitter: float = 0.25
    idle_backoff_max: float = 4.0
    retry_seconds: float = 300.0
    priority: int = 100
    enabled: bool = True

    @property
    def path_id(self) -> str:
        return f"directory:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "directory",
            "name": self.name,
            "path": str(self.path),
            "peers": list(self.peers),
            "mode": self.mode,
            "interval": self.interval,
            "jitter": self.jitter,
            "idle_backoff_max": self.idle_backoff_max,
            "retry_seconds": self.retry_seconds,
            "priority": self.priority,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], *, home: Path
    ) -> "DirectoryCarrierConfig":
        if str(value.get("type", "directory")) != "directory":
            raise ValueError("unsupported configured carrier type")
        name = str(value.get("name", "directory")).strip()
        if (
            not name
            or len(name) > 64
            or not all(ch.isalnum() or ch in "-_" for ch in name)
        ):
            raise ValueError("invalid directory carrier name")
        raw_path = Path(str(value["path"])).expanduser()
        path = raw_path if raw_path.is_absolute() else home / raw_path
        mode = str(value.get("mode", "fallback")).strip().lower()
        if mode not in {"fallback", "always", "receive-only"}:
            raise ValueError("invalid directory carrier mode")
        return cls(
            name=name,
            path=path.resolve(),
            peers=tuple(
                sorted({str(item) for item in value.get("peers", []) if str(item)})
            ),
            mode=mode,
            interval=max(0.2, float(value.get("interval", 2.0))),
            jitter=max(0.0, min(float(value.get("jitter", 0.25)), 0.9)),
            idle_backoff_max=max(
                1.0, min(float(value.get("idle_backoff_max", 4.0)), 64.0)
            ),
            retry_seconds=max(0.0, float(value.get("retry_seconds", 300.0))),
            priority=max(0, min(int(value.get("priority", 100)), 10000)),
            enabled=bool(value.get("enabled", True)),
        )


@dataclass(frozen=True)
class RoutingConfig:
    direct_failure_threshold: int = 2
    direct_recovery_threshold: int = 3
    carrier_failure_threshold: int = 2
    carrier_recovery_threshold: int = 3
    carrier_replica_count: int = 1
    direct_retry_interval: float = 5.0
    direct_race_width: int = 1
    direct_race_delay: float = 0.15
    direct_idle_probe_interval: float = 60.0
    direct_probe_jitter: float = 0.35
    direct_idle_backoff_max: float = 4.0
    fallback_probe_interval: float = 5.0
    fallback_probe_jitter: float = 0.35
    switch_cooldown: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "direct_failure_threshold": self.direct_failure_threshold,
            "direct_recovery_threshold": self.direct_recovery_threshold,
            "carrier_failure_threshold": self.carrier_failure_threshold,
            "carrier_recovery_threshold": self.carrier_recovery_threshold,
            "carrier_replica_count": self.carrier_replica_count,
            "direct_retry_interval": self.direct_retry_interval,
            "direct_race_width": self.direct_race_width,
            "direct_race_delay": self.direct_race_delay,
            "direct_idle_probe_interval": self.direct_idle_probe_interval,
            "direct_probe_jitter": self.direct_probe_jitter,
            "direct_idle_backoff_max": self.direct_idle_backoff_max,
            "fallback_probe_interval": self.fallback_probe_interval,
            "fallback_probe_jitter": self.fallback_probe_jitter,
            "switch_cooldown": self.switch_cooldown,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoutingConfig":
        return cls(
            direct_failure_threshold=max(
                1, min(int(value.get("direct_failure_threshold", 2)), 20)
            ),
            direct_recovery_threshold=max(
                1, min(int(value.get("direct_recovery_threshold", 3)), 20)
            ),
            carrier_failure_threshold=max(
                1, min(int(value.get("carrier_failure_threshold", 2)), 20)
            ),
            carrier_recovery_threshold=max(
                1, min(int(value.get("carrier_recovery_threshold", 3)), 20)
            ),
            carrier_replica_count=max(
                1, min(int(value.get("carrier_replica_count", 1)), 4)
            ),
            direct_retry_interval=max(
                0.2, min(float(value.get("direct_retry_interval", 5.0)), 3600.0)
            ),
            direct_race_width=max(
                1, min(int(value.get("direct_race_width", 1)), 4)
            ),
            direct_race_delay=max(
                0.0, min(float(value.get("direct_race_delay", 0.15)), 5.0)
            ),
            direct_idle_probe_interval=max(
                1.0, min(float(value.get("direct_idle_probe_interval", 60.0)), 86400.0)
            ),
            direct_probe_jitter=max(
                0.0, min(float(value.get("direct_probe_jitter", 0.35)), 0.9)
            ),
            direct_idle_backoff_max=max(
                1.0, min(float(value.get("direct_idle_backoff_max", 4.0)), 64.0)
            ),
            fallback_probe_interval=max(
                0.5, min(float(value.get("fallback_probe_interval", 5.0)), 3600.0)
            ),
            fallback_probe_jitter=max(
                0.0, min(float(value.get("fallback_probe_jitter", 0.35)), 0.9)
            ),
            switch_cooldown=max(0.0, float(value.get("switch_cooldown", 30.0))),
        )


@dataclass(frozen=True)
class WebDAVCarrierConfig:
    name: str
    base_url: str
    peers: tuple[str, ...] = ()
    mode: str = "fallback"
    interval: float = 5.0
    jitter: float = 0.25
    idle_backoff_max: float = 4.0
    retry_seconds: float = 300.0
    priority: int = 200
    enabled: bool = True
    timeout: float = 15.0
    bearer_env: str = ""
    username_env: str = ""
    password_env: str = ""
    allow_insecure_http: bool = False

    @property
    def path_id(self) -> str:
        return f"webdav:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "webdav",
            "name": self.name,
            "base_url": self.base_url,
            "peers": list(self.peers),
            "mode": self.mode,
            "interval": self.interval,
            "jitter": self.jitter,
            "idle_backoff_max": self.idle_backoff_max,
            "retry_seconds": self.retry_seconds,
            "priority": self.priority,
            "enabled": self.enabled,
            "timeout": self.timeout,
            "bearer_env": self.bearer_env,
            "username_env": self.username_env,
            "password_env": self.password_env,
            "allow_insecure_http": self.allow_insecure_http,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WebDAVCarrierConfig":
        name = str(value.get("name", "webdav")).strip()
        if (
            not name
            or len(name) > 64
            or not all(ch.isalnum() or ch in "-_" for ch in name)
        ):
            raise ValueError("invalid WebDAV carrier name")
        base_url = str(value["base_url"]).strip().rstrip("/")
        parsed = urlsplit(base_url)
        allow_insecure = bool(value.get("allow_insecure_http", False))
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("WebDAV base URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "WebDAV URL must not contain credentials, query, or fragment"
            )
        if parsed.scheme == "http":
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not allow_insecure or not loopback:
                raise ValueError(
                    "plain HTTP WebDAV is allowed only for explicit loopback tests"
                )
        mode = str(value.get("mode", "fallback")).strip().lower()
        if mode not in {"fallback", "always", "receive-only"}:
            raise ValueError("invalid WebDAV carrier mode")
        env_names = {
            key: str(value.get(key, "")).strip()
            for key in ("bearer_env", "username_env", "password_env")
        }
        if any(name and not _ENV_RE.match(name) for name in env_names.values()):
            raise ValueError("invalid WebDAV credential environment variable name")
        if bool(env_names["username_env"]) != bool(env_names["password_env"]):
            raise ValueError(
                "WebDAV basic authentication requires both username and password env names"
            )
        return cls(
            name=name,
            base_url=base_url,
            peers=tuple(
                sorted({str(item) for item in value.get("peers", []) if str(item)})
            ),
            mode=mode,
            interval=max(0.5, float(value.get("interval", 5.0))),
            jitter=max(0.0, min(float(value.get("jitter", 0.25)), 0.9)),
            idle_backoff_max=max(
                1.0, min(float(value.get("idle_backoff_max", 4.0)), 64.0)
            ),
            retry_seconds=max(0.0, float(value.get("retry_seconds", 300.0))),
            priority=max(0, min(int(value.get("priority", 200)), 10000)),
            enabled=bool(value.get("enabled", True)),
            timeout=max(1.0, min(float(value.get("timeout", 15.0)), 120.0)),
            bearer_env=env_names["bearer_env"],
            username_env=env_names["username_env"],
            password_env=env_names["password_env"],
            allow_insecure_http=allow_insecure,
        )


@dataclass(frozen=True)
class AhubCarrierConfig:
    name: str
    base_url: str
    peers: tuple[str, ...] = ()
    mode: str = "fallback"
    interval: float = 2.0
    jitter: float = 0.25
    idle_backoff_max: float = 4.0
    retry_seconds: float = 300.0
    priority: int = 50
    enabled: bool = True
    timeout: float = 15.0
    claim_lease_seconds: float = 30.0
    allow_insecure_http: bool = False
    live_relay_enabled: bool = False
    relay_reservation_ttl_seconds: float = 900.0
    relay_session_seconds: float = 300.0
    relay_bytes_each_direction: int = 64 * 1024 * 1024
    relay_listener_retry_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.live_relay_enabled and not self.peers:
            raise ValueError("live Ahub Relay requires explicit peers")
        if not 30.0 <= self.relay_reservation_ttl_seconds <= 900.0:
            raise ValueError("Ahub Relay reservation TTL is outside limits")
        if not 1.0 <= self.relay_session_seconds <= 300.0:
            raise ValueError("Ahub Relay session duration is outside limits")
        if not 1 <= self.relay_bytes_each_direction <= 64 * 1024 * 1024:
            raise ValueError("Ahub Relay byte allowance is outside limits")
        if not 0.2 <= self.relay_listener_retry_seconds <= 60.0:
            raise ValueError("Ahub Relay listener retry is outside limits")

    @property
    def path_id(self) -> str:
        return f"ahub:{self.name}"

    @property
    def relay_path_id(self) -> str:
        return f"ahub-relay:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "ahub",
            "name": self.name,
            "base_url": self.base_url,
            "peers": list(self.peers),
            "mode": self.mode,
            "interval": self.interval,
            "jitter": self.jitter,
            "idle_backoff_max": self.idle_backoff_max,
            "retry_seconds": self.retry_seconds,
            "priority": self.priority,
            "enabled": self.enabled,
            "timeout": self.timeout,
            "claim_lease_seconds": self.claim_lease_seconds,
            "allow_insecure_http": self.allow_insecure_http,
            "live_relay_enabled": self.live_relay_enabled,
            "relay_reservation_ttl_seconds": (
                self.relay_reservation_ttl_seconds
            ),
            "relay_session_seconds": self.relay_session_seconds,
            "relay_bytes_each_direction": self.relay_bytes_each_direction,
            "relay_listener_retry_seconds": (
                self.relay_listener_retry_seconds
            ),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AhubCarrierConfig":
        if str(value.get("type", "")) != "ahub":
            raise ValueError("unsupported configured carrier type")
        name = str(value.get("name", "ahub")).strip()
        if (
            not name
            or len(name) > 64
            or not all(ch.isalnum() or ch in "-_" for ch in name)
        ):
            raise ValueError("invalid Ahub carrier name")
        base_url = str(value["base_url"]).strip().rstrip("/")
        parsed = urlsplit(base_url)
        allow_insecure = bool(value.get("allow_insecure_http", False))
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Ahub base URL must be an origin HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Ahub URL must not contain credentials, path, query, or fragment"
            )
        if parsed.scheme == "http":
            loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not allow_insecure or not loopback:
                raise ValueError(
                    "plain HTTP Ahub is allowed only for explicit loopback tests"
                )
        mode = str(value.get("mode", "fallback")).strip().lower()
        if mode not in {"fallback", "always", "receive-only"}:
            raise ValueError("invalid Ahub carrier mode")
        return cls(
            name=name,
            base_url=base_url,
            peers=tuple(
                sorted({str(item) for item in value.get("peers", []) if str(item)})
            ),
            mode=mode,
            interval=max(0.5, float(value.get("interval", 2.0))),
            jitter=max(0.0, min(float(value.get("jitter", 0.25)), 0.9)),
            idle_backoff_max=max(
                1.0, min(float(value.get("idle_backoff_max", 4.0)), 64.0)
            ),
            retry_seconds=max(0.0, float(value.get("retry_seconds", 300.0))),
            priority=max(0, min(int(value.get("priority", 50)), 10000)),
            enabled=bool(value.get("enabled", True)),
            timeout=max(1.0, min(float(value.get("timeout", 15.0)), 120.0)),
            claim_lease_seconds=max(
                5.0,
                min(float(value.get("claim_lease_seconds", 30.0)), 300.0),
            ),
            allow_insecure_http=allow_insecure,
            live_relay_enabled=bool(
                value.get("live_relay_enabled", False)
            ),
            relay_reservation_ttl_seconds=max(
                30.0,
                min(
                    float(
                        value.get(
                            "relay_reservation_ttl_seconds",
                            900.0,
                        )
                    ),
                    900.0,
                ),
            ),
            relay_session_seconds=max(
                1.0,
                min(
                    float(value.get("relay_session_seconds", 300.0)),
                    300.0,
                ),
            ),
            relay_bytes_each_direction=max(
                1,
                min(
                    int(
                        value.get(
                            "relay_bytes_each_direction",
                            64 * 1024 * 1024,
                        )
                    ),
                    64 * 1024 * 1024,
                ),
            ),
            relay_listener_retry_seconds=max(
                0.2,
                min(
                    float(
                        value.get(
                            "relay_listener_retry_seconds",
                            2.0,
                        )
                    ),
                    60.0,
                ),
            ),
        )


@dataclass(frozen=True)
class NodeConfig:
    home: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 4242
    advertise: tuple[str, ...] = ()
    locator_contexts: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    sync_interval: float = 2.0
    sync_jitter: float = 0.2
    max_batch: int = 128
    max_hops: int = 8
    padding_min: int = 512
    prekey_policy: str = "prefer"
    prekey_auto_enabled: bool = True
    prekey_low_watermark: int = 64
    prekey_batch_size: int = 256
    prekey_request_interval: float = 900.0
    prekey_ttl_days: float = 30.0
    listen_enabled: bool = True
    direct_enabled: bool = True
    direct_proxy: DirectProxyConfig | None = None
    direct_dialers: tuple[DirectDialerConfig, ...] = ()
    directory_carriers: tuple[DirectoryCarrierConfig, ...] = ()
    webdav_carriers: tuple[WebDAVCarrierConfig, ...] = ()
    ahub_carriers: tuple[AhubCarrierConfig, ...] = ()
    routing: RoutingConfig = RoutingConfig()

    @property
    def identity_path(self) -> Path:
        return self.home / "identity.json"

    @property
    def peers_path(self) -> Path:
        return self.home / "peers.json"

    @property
    def database_path(self) -> Path:
        current = self.home / "anet.sqlite3"
        legacy = self.home / "ainet.sqlite3"
        if not current.exists() and legacy.exists():
            legacy.replace(current)
        return current

    @property
    def control_database_path(self) -> Path:
        return self.home / "control.sqlite3"

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    def effective_addresses(self) -> tuple[str, ...]:
        if not self.listen_enabled:
            return ()
        if self.advertise:
            return self.advertise
        # Port zero asks the OS for an ephemeral test listener; it is not a
        # remotely usable locator and must never be published in a Peer Card.
        if self.listen_port == 0:
            return ()
        host = self.listen_host
        rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return (f"tls://{rendered}:{self.listen_port}",)

    def effective_direct_dialers(self) -> tuple[DirectDialerConfig, ...]:
        if self.direct_dialers:
            return tuple(
                sorted(
                    (dialer for dialer in self.direct_dialers if dialer.enabled),
                    key=lambda item: (item.priority, item.name),
                )
            )
        if self.direct_proxy is not None:
            return (
                DirectDialerConfig(
                    name="legacy-proxy",
                    priority=0,
                    proxy=self.direct_proxy,
                ),
            )
        return (DirectDialerConfig(name="raw", priority=0),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "advertise": list(self.advertise),
            "locator_contexts": list(self.locator_contexts),
            "capabilities": list(self.capabilities),
            "sync_interval": self.sync_interval,
            "sync_jitter": self.sync_jitter,
            "max_batch": self.max_batch,
            "max_hops": self.max_hops,
            "padding_min": self.padding_min,
            "prekey_policy": self.prekey_policy,
            "prekey_auto_enabled": self.prekey_auto_enabled,
            "prekey_low_watermark": self.prekey_low_watermark,
            "prekey_batch_size": self.prekey_batch_size,
            "prekey_request_interval": self.prekey_request_interval,
            "prekey_ttl_days": self.prekey_ttl_days,
            "listen_enabled": self.listen_enabled,
            "direct_enabled": self.direct_enabled,
            "direct_proxy": self.direct_proxy.to_dict() if self.direct_proxy else None,
            "direct_dialers": [
                dialer.to_dict() for dialer in self.direct_dialers
            ],
            "carriers": [
                carrier.to_dict()
                for carrier in (
                    *self.directory_carriers,
                    *self.webdav_carriers,
                    *self.ahub_carriers,
                )
            ],
            "routing": self.routing.to_dict(),
        }

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        atomic_json(self.config_path, self.to_dict())

    @classmethod
    def load(cls, home: Path) -> "NodeConfig":
        home = Path(home).expanduser().resolve()
        path = home / "config.json"
        if not path.exists():
            raise FileNotFoundError(f"Anet node is not initialized: {home}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if int(value.get("version", 0)) != CONFIG_VERSION:
            raise ValueError("unsupported Anet config version")
        carrier_values = [
            item for item in value.get("carriers", []) if isinstance(item, dict)
        ]
        carriers = tuple(
            DirectoryCarrierConfig.from_dict(item, home=home)
            for item in carrier_values
            if str(item.get("type", "directory")) == "directory"
        )
        webdav_carriers = tuple(
            WebDAVCarrierConfig.from_dict(item)
            for item in carrier_values
            if str(item.get("type", "directory")) == "webdav"
        )
        ahub_carriers = tuple(
            AhubCarrierConfig.from_dict(item)
            for item in carrier_values
            if str(item.get("type", "directory")) == "ahub"
        )
        known_types = {"directory", "webdav", "ahub"}
        unknown_types = {
            str(item.get("type", "directory")) for item in carrier_values
        } - known_types
        if unknown_types:
            raise ValueError(
                f"unsupported configured carrier type(s): {', '.join(sorted(unknown_types))}"
            )
        names = [
            carrier.name
            for carrier in (*carriers, *webdav_carriers, *ahub_carriers)
        ]
        if len(names) != len(set(names)):
            raise ValueError("configured carrier names must be unique")
        dialer_values = value.get("direct_dialers", [])
        if not isinstance(dialer_values, list):
            raise ValueError("direct_dialers must be a list")
        dialers = tuple(
            DirectDialerConfig.from_dict(item)
            for item in dialer_values
            if isinstance(item, dict)
        )
        if len(dialers) != len(dialer_values):
            raise ValueError("each direct dialer must be an object")
        dialer_names = [dialer.name for dialer in dialers]
        if len(dialer_names) != len(set(dialer_names)):
            raise ValueError("configured direct dialer names must be unique")
        prekey_policy = str(value.get("prekey_policy", "prefer")).strip().lower()
        if prekey_policy not in {"prefer", "require", "disable"}:
            raise ValueError("prekey policy must be prefer, require, or disable")
        return cls(
            home=home,
            listen_host=str(value.get("listen_host", "127.0.0.1")),
            listen_port=int(value.get("listen_port", 4242)),
            advertise=tuple(
                parse_locator(str(item)).raw for item in value.get("advertise", [])
            ),
            locator_contexts=tuple(
                sorted(
                    {
                        validate_locator_context(str(item))
                        for item in value.get("locator_contexts", [])
                    }
                )
            ),
            capabilities=tuple(
                sorted(
                    set(DEFAULT_CAPABILITIES)
                    | {str(item) for item in value.get("capabilities", []) if str(item)}
                )
            ),
            sync_interval=max(0.2, float(value.get("sync_interval", 2.0))),
            sync_jitter=max(0.0, min(float(value.get("sync_jitter", 0.2)), 0.9)),
            max_batch=max(1, min(int(value.get("max_batch", 128)), 1024)),
            max_hops=max(1, min(int(value.get("max_hops", 8)), 32)),
            padding_min=max(256, min(int(value.get("padding_min", 512)), 65536)),
            prekey_policy=prekey_policy,
            prekey_auto_enabled=bool(value.get("prekey_auto_enabled", True)),
            prekey_low_watermark=max(
                1, min(int(value.get("prekey_low_watermark", 64)), 999)
            ),
            prekey_batch_size=max(
                1, min(int(value.get("prekey_batch_size", 256)), 1000)
            ),
            prekey_request_interval=max(
                30.0,
                min(float(value.get("prekey_request_interval", 900.0)), 86400.0),
            ),
            prekey_ttl_days=max(
                1.0, min(float(value.get("prekey_ttl_days", 30.0)), 365.0)
            ),
            listen_enabled=bool(value.get("listen_enabled", True)),
            direct_enabled=bool(value.get("direct_enabled", True)),
            direct_proxy=(
                DirectProxyConfig.from_dict(value["direct_proxy"])
                if isinstance(value.get("direct_proxy"), dict)
                else None
            ),
            direct_dialers=dialers,
            directory_carriers=carriers,
            webdav_carriers=webdav_carriers,
            ahub_carriers=ahub_carriers,
            routing=RoutingConfig.from_dict(
                value.get("routing", {})
                if isinstance(value.get("routing", {}), dict)
                else {}
            ),
        )


def initialize_node(
    home: Path,
    *,
    label: str,
    listen_host: str = "127.0.0.1",
    listen_port: int = 4242,
    advertise: list[str] | tuple[str, ...] = (),
    locator_contexts: list[str] | tuple[str, ...] = (),
) -> NodeConfig:
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    config = NodeConfig(
        home=home,
        listen_host=listen_host,
        listen_port=int(listen_port),
        advertise=tuple(parse_locator(item).raw for item in advertise),
        locator_contexts=tuple(
            sorted({validate_locator_context(item) for item in locator_contexts})
        ),
    )
    if config.identity_path.exists() or config.config_path.exists():
        raise FileExistsError(f"Anet node already exists: {home}")
    identity = Identity.generate(label)
    identity.save(config.identity_path)
    identity.ensure_tls_material(home)
    atomic_json(config.peers_path, {"version": 1, "peers": []}, private=True)
    config.save()
    return config
