#!/usr/bin/env python3
"""Sign one Anet JSON control page with an offline publisher identity."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from anet.encoding import atomic_json, b64e
from anet.identity import Identity
from anet.remote_control import CONTROL_DEFAULT_TTL_MS, sign_control_page


def main() -> int:
    parser = argparse.ArgumentParser(
        description="sign an Anet control page for a pinned supervisor"
    )
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument(
        "--expires-seconds",
        type=int,
        default=CONTROL_DEFAULT_TTL_MS // 1000,
    )
    args = parser.parse_args()
    if args.expires_seconds <= 0:
        raise SystemExit("--expires-seconds must be positive")
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read control page: {args.input}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("control page payload must be a JSON object")
    identity = Identity.load(args.identity)
    now_ms = int(time.time() * 1000)
    signed = sign_control_page(
        payload,
        identity,
        key_id=args.key_id,
        issued_ms=now_ms,
        expires_ms=now_ms + args.expires_seconds * 1000,
    )
    atomic_json(args.output, signed)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "key_id": args.key_id,
                "public_key": b64e(identity.sign_public),
                "expires_ms": signed["_anet_control"]["expires_ms"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
