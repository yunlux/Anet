from __future__ import annotations

import os
from pathlib import Path

import pytest

from amesh.identity import LocalIdentity, identity_key_path, platform_actor_id


def test_identity_key_path_is_stable(tmp_path: Path) -> None:
    assert identity_key_path(tmp_path) == tmp_path / "amesh-identity.key"


def test_local_identity_creates_private_key_file(tmp_path: Path) -> None:
    identity = LocalIdentity(tmp_path)
    key_path = tmp_path / "amesh-identity.key"
    assert key_path.is_file()
    raw = key_path.read_bytes()
    assert len(raw) == 32
    if os.name == "posix":
        assert key_path.stat().st_mode & 0o777 == 0o600
    assert identity.identity_id.startswith("id_")
    assert len(identity.identity_id) == 3 + 32


def test_local_identity_loads_existing_key(tmp_path: Path) -> None:
    first = LocalIdentity(tmp_path)
    second = LocalIdentity(tmp_path)
    assert second.identity_id == first.identity_id
    assert second.pseudonym("actor", "alice") == first.pseudonym("actor", "alice")


def test_local_identity_rejects_invalid_key_length(tmp_path: Path) -> None:
    (tmp_path / "amesh-identity.key").write_bytes(b"too-short")
    with pytest.raises(ValueError, match="identity key is invalid"):
        LocalIdentity(tmp_path)


def test_pseudonym_is_deterministic_and_namespace_scoped(tmp_path: Path) -> None:
    identity = LocalIdentity(tmp_path)
    assert identity.pseudonym("actor", "alice") == identity.pseudonym(
        "actor", "alice"
    )
    assert identity.pseudonym("actor", "alice") != identity.pseudonym(
        "actor", "bob"
    )
    assert identity.pseudonym("actor", "alice") != identity.pseudonym(
        "event", "alice"
    )


def test_pseudonym_normalizes_namespace_case(tmp_path: Path) -> None:
    identity = LocalIdentity(tmp_path)
    assert identity.pseudonym("Actor", "alice") == identity.pseudonym(
        "actor", "alice"
    )


def test_pseudonym_rejects_out_of_limits(tmp_path: Path) -> None:
    identity = LocalIdentity(tmp_path)
    with pytest.raises(ValueError, match="outside limits"):
        identity.pseudonym("", "alice")
    with pytest.raises(ValueError, match="outside limits"):
        identity.pseudonym("a" * 65, "alice")
    with pytest.raises(ValueError, match="outside limits"):
        identity.pseudonym("actor", "x" * 4097)


def test_pseudonym_is_not_reversible_from_output(tmp_path: Path) -> None:
    identity = LocalIdentity(tmp_path)
    pseudonym = identity.pseudonym("actor", "alice")
    assert pseudonym != "alice"
    assert "alice" not in pseudonym


def test_platform_actor_id_is_deterministic_and_namespaced() -> None:
    first = platform_actor_id(
        "discord",
        namespace_actor_id="ns-1",
        platform_actor_key="user-1",
    )
    second = platform_actor_id(
        "discord",
        namespace_actor_id="ns-1",
        platform_actor_key="user-1",
    )
    assert first == second
    assert first.startswith("actor_")
    assert first != platform_actor_id(
        "discord",
        namespace_actor_id="ns-1",
        platform_actor_key="user-2",
    )
    assert first != platform_actor_id(
        "discord",
        namespace_actor_id="ns-2",
        platform_actor_key="user-1",
    )
    assert first != platform_actor_id(
        "telegram",
        namespace_actor_id="ns-1",
        platform_actor_key="user-1",
    )


def test_platform_actor_id_normalizes_platform_case() -> None:
    assert platform_actor_id(
        "Discord",
        namespace_actor_id="ns",
        platform_actor_key="k",
    ) == platform_actor_id(
        "discord",
        namespace_actor_id="ns",
        platform_actor_key="k",
    )


def test_identity_ids_differ_between_homes(tmp_path: Path) -> None:
    first = LocalIdentity(tmp_path / "a")
    second = LocalIdentity(tmp_path / "b")
    assert first.identity_id != second.identity_id
    assert first.pseudonym("actor", "alice") != second.pseudonym(
        "actor", "alice"
    )
