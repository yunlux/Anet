from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import msgpack


MAX_WIRE_BYTES = 16 * 1024 * 1024


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, bytearray):
        return bytes(value)
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def pack(value: Any) -> bytes:
    return msgpack.packb(value, use_bin_type=True, strict_types=False)


def canonical_pack(value: Any) -> bytes:
    return msgpack.packb(_canonical(value), use_bin_type=True, strict_types=False)


def unpack(raw: bytes) -> Any:
    if len(raw) > MAX_WIRE_BYTES:
        raise ValueError("wire object exceeds size limit")
    return msgpack.unpackb(raw, raw=False, strict_map_key=False)


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    value = str(text).strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def atomic_write(path: Path, data: bytes, *, private: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_json(path: Path, value: Any, *, private: bool = False) -> None:
    atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        private=private,
    )
