---
type: Verification Runbook
title: "Anet staged verification gates"
description: "Fail-closed verification runbook for Anet static checks, automated tests, identity and PeerBook integrity, node-home and listener isolation, CLI diagnostics, direct and carrier delivery, recovery, revocation, release deployment, and physical-device LAN validation."
tags: [anet, testing, verification, security, networking, operations]
resource: "VERIFICATION.md"
---

# Anet staged verification gates

Use these gates in order. Each later gate assumes all earlier gates passed, and a failure stops promotion, deployment, or the claim being tested. Preserve machine-readable outputs and timestamps in a restricted evidence directory, but publish only redacted summaries: never include local paths, complete Node IDs, IP addresses, credentials, private keys, peer-card key material, carrier or service names, or environment-specific secrets.

This runbook validates the identity, transport, storage, and acknowledgement boundaries in the [architecture overview](../architecture/overview.md), and it applies the home ownership, backup, deployment, and recovery rules in [onboarding and recovery](../operations/onboarding-and-recovery.md). MCP deployments must also retain the [stdio adapter's one-profile-per-home boundary](../integrations/mcp.md).

## Evidence conventions and placeholders

Use placeholders consistently in commands and private notes:

- `<SOURCE_ROOT>`, `<VENV>`, `<EVIDENCE_DIR>`, `<REPORT>`
- `<HOME_A>`, `<HOME_B>`, `<HOME_DEVICE_A>`, `<HOME_DEVICE_B>`
- `<NODE_A>`, `<NODE_B>`, `<LAN_ADDRESS_A>`, `<LAN_ADDRESS_B>`
- `<PORT_A>`, `<PORT_B>`, `<CARRIER_TARGET>`, `<CARRIER_NAME>`
- `<SERVICE>`, `<WHEEL>`, `<SDIST>`, `<ROLLBACK_WHEEL>`, `<EXPECTED_TESTS>`

For every gate record: UTC timestamp, code/package version, command or procedure, exit status, sanitized result, and reviewer. Keep raw JSON/JSONL private. A passing receipt must belong to the probe under test; a custody or relay ACK alone is not end-to-end delivery (`VERIFICATION.md:19-21`).

## Gate 1 — static checks and isolated unit tests

Run checks from a clean environment using Python 3.11 or newer, matching `pyproject.toml:5-18,30-31`:

```text
<VENV_PYTHON> -m ruff check src tests scripts/wsl_release_gate.py
<VENV_PYTHON> -m pytest -q
```

The complete suite is the primary gate. Representative security and lifecycle coverage includes:

- signed Card integrity and packet tamper rejection — `tests/test_crypto.py`;
- signed, expiring, challenge-bound pairing and both PeerBooks — `tests/test_pairing.py`;
- direct routing, carrier fallback, and hysteretic recovery — `tests/test_routing.py`, `tests/test_directory_carrier.py`, and `tests/test_webdav_carrier.py`;
- deny-first, immediate, atomic, and non-reversible local revocation — `tests/test_revocation.py`;
- CLI diagnostics and configuration — `tests/test_cli.py`;
- artifact extraction, private reports, and non-leaking summaries — `tests/test_wsl_release_gate.py`.

**Expected evidence:** Ruff reports success; pytest exits zero with no failures, errors, skips that conceal required coverage, or collection warnings requiring investigation. Record the observed test count rather than copying a historical count from `VERIFICATION.md`.

**Stop closed:** stop on any nonzero exit, unexpected deselection/skip, collection failure, dependency/import mismatch, or unexplained test-count decrease. Do not compensate by running only the failing module or by lowering an expected release count.

## Gate 2 — Card signature and PeerBook validation

Use disposable or intended node homes; never repair trust by copying another runtime's private files.

1. Export each signed public Card and verify it with the normal Card parser or `peer-add` flow.
2. Confirm each Card's derived Node ID matches the identity that produced it.
3. Confirm A's PeerBook contains B's expected signed Card and B's contains A's, with no self-entry, duplicate identity, key mismatch, or locally revoked identity.
4. For asynchronous onboarding, prefer `pair-offer`, `pair-accept`, and `pair-complete`; verify expiration, explicit acceptance, response-to-offer binding, and the final reciprocal PeerBooks.

`PeerBook.add()` verifies signatures, rejects the local identity, rejects revoked identities, and rejects key changes under an existing Node ID (`src/anet/peers.py:57-67`).

**Expected evidence:** sanitized Card verification success, reciprocal PeerBook membership counts, and a reviewer-approved mapping of logical runtime labels to redacted Node ID fingerprints. Keep complete Cards and IDs private.

**Stop closed:** stop on signature failure, expired or mismatched pairing material, self-pairing, one-sided trust, unknown extra peers, Node ID/key mismatch, or any attempt to re-add a revoked identity. Do not infer identity from a label, address, hostname, or service name.

## Gate 3 — one home per identity and distinct listener ports

Audit all persistent processes before starting network tests:

| Invariant | Required check |
|---|---|
| One owner | Each persistent runtime or MCP profile has one dedicated `<HOME_…>`; no home is shared by two logical identities. |
| One identity | Each home has its own `identity.json`, TLS key, database, configuration, PeerBook, and revocation state. |
| Stable mapping | Repeated status/doctor checks for a home resolve to the same Node ID; different homes resolve to different Node IDs. |
| Distinct listeners | Concurrent nodes on the same host or mirrored Windows/WSL network use different `<PORT_A>` and `<PORT_B>`. |
| Address truth | Advertised addresses match the interface and port reachable by the intended peer. |

The home file boundary is defined by `src/anet/config.py:357-375`; `initialize_node()` refuses an already initialized home (`src/anet/config.py:502-525`). Compare private file hashes only to detect unintended changes—never publish those hashes with file locations or identity metadata.

**Expected evidence:** a redacted inventory with one row per runtime, unique home token, unique Node ID fingerprint, configured listener token, observed listener owner, and no collisions. Capture operating-system listener output privately before and after startup.

**Stop closed:** stop if homes, identities, or listener ports collide; if a listener is owned by an unexpected process; if the advertised endpoint differs from the observed listener; or if private identity/state was copied between runtimes. Network-interface mirroring does not permit shared identity or shared ports.

## Gate 4 — `doctor`

Run against every intended home while preserving its JSON privately:

```text
anet --home <HOME_A> doctor
anet --home <HOME_B> doctor
```

`doctor` loads the configuration and identity, verifies the current signed Card, ensures TLS material, loads the PeerBook and packet store, and reports trust, store, and prekey state (`src/anet/cli.py:1022-1054`).

**Expected evidence:** exit zero and JSON with `ok: true`, successful identity/Card/TLS checks, expected trusted-peer count, readable store status, and a plausible prekey summary. Redact `home`, `node_id`, certificate/key paths, fingerprints, and peer details from shared summaries.

**Stop closed:** stop on any exception, nonzero exit, Card/TLS failure, unreadable store, unexpected peer count, unexpected queue/rejection/untrusted state, or identity mismatch with Gate 3. `doctor` is a local integrity check; it does not prove peer reachability.

## Gate 5 — direct end-to-end probe and benchmark

With both intended services active and direct routing enabled, probe the pinned destination:

```text
anet --home <HOME_A> probe <NODE_B> --timeout <SECONDS> --carrier-grace <SECONDS>
anet --home <HOME_A> benchmark <NODE_B> --count <COUNT> --out <EVIDENCE_DIR>/<RUN>.jsonl --min-success-rate <RATE>
```

`probe` prints JSON and exits `0` only when `result.ok` is true; otherwise it exits `2`. `benchmark` writes repeatable JSONL observations and exits `0` only when its observed success rate meets the configured threshold (`src/anet/cli.py:651-697,1307-1339`).

**Expected evidence:** the direct probe is acknowledged end to end, identifies `direct` as the delivery path, has a destination receipt, and leaves expected queue/rejection/untrusted counters. The benchmark records its count, success rate, latency summary, selected path, payload/QoS parameters, and sanitized JSONL artifact.

**Stop closed:** stop on timeout, exit `2`, absent destination receipt, wrong destination, unexpected delivery path, success below the predeclared threshold, new rejection/untrusted entries, or non-draining pending work. Do not relax thresholds after observing results.

## Gate 6 — carrier fallback with direct listening unavailable

This gate proves transport-independent delivery, not direct reachability.

1. Configure an approved directory or WebDAV fallback using `<CARRIER_NAME>` and `<CARRIER_TARGET>`; keep credentials in environment variables, not URLs or evidence.
2. Disable direct listening/dialing for the controlled test and verify that the intended Anet listener count is zero.
3. Run a probe from A to B and allow the configured carrier grace interval.
4. For an asynchronous carrier, preserve evidence for packet transfer, custody ACK, destination receipt, and receipt ACK rounds.

The routing tests exercise failure thresholds and hysteretic return to direct (`tests/test_routing.py`). Directory and WebDAV tests cover carrier framing, tamper rejection, and no-listener delivery.

**Expected evidence:** no direct listeners during the test; acknowledged delivery through `directory:<REDACTED>` or `webdav:<REDACTED>`; destination receipt; trusted inbox processing; no rejected carrier frames; and drained pending work at both ends. Shared summaries should state only the carrier type, not its local target or configured name.

**Stop closed:** stop if any direct listener remains, the result silently uses `direct`, a receipt is absent, only a relay/custody ACK exists, plaintext identity/message metadata appears in carrier storage, tampered frames are accepted, or pending/rejection/untrusted counters are unexplained.

## Gate 7 — restart, route recovery, and state preservation

Restore direct configuration, restart each managed process through its normal supervisor, and wait only for the predeclared startup timeout. Then repeat `doctor`, listener ownership checks, direct probes, and a short benchmark.

The adaptive router deliberately requires consecutive observations and cooldown/hysteresis before switching paths (`tests/test_routing.py:40-98`). Do not treat the first successful direct attempt as proof that the selected route has recovered.

**Expected evidence:** supervisor reports active/healthy; the expected process owns the expected distinct port; Node IDs, PeerBooks, revocations, protected-state hashes, and prekey generations remain unchanged; store integrity is preserved; and routing returns to direct only after the configured recovery threshold. Record any fallback-to-direct transition and its reason.

**Stop closed:** stop on startup timeout, crash loop, unexpected process owner, identity/trust/revocation/prekey mutation, database integrity failure, lost queued work, premature route switching, or failure to regain acknowledged direct delivery. Restore the last known-good state before continuing.

## Gate 8 — revocation on disposable peers

Never test destructive revocation against a real peer. Create disposable A/B homes, establish trust, queue representative peer-scoped work, and revoke B from A using B's complete Node ID twice as required by the CLI:

```text
anet --home <DISPOSABLE_HOME_A> peer-revoke <DISPOSABLE_NODE_B> --confirm <DISPOSABLE_NODE_B> --reason <TEST_REASON>
anet --home <DISPOSABLE_HOME_A> peer-revocations
```

The implementation persists the deny record before removing positive trust, reloads trust at runtime boundaries, and forbids ordinary re-add (`src/anet/peers.py:85-126`). Tests also require atomic cleanup of queued packets, inbox trust, consumer claims, peer-scoped prekeys, routes, and path metrics (`tests/test_revocation.py`).

**Expected evidence:** partial confirmation is rejected without changing trust; exact confirmation succeeds; no restart is required; the revocation ledger contains the disposable peer; queued peer work and key/routing state are retired; an already-running trust view rejects the peer; repeated revocation is idempotent; and ordinary `peer-add` cannot restore it.

**Stop closed:** stop if partial confirmation succeeds, deny state is absent after a simulated restart, the peer remains trusted, cleanup is partial, a database failure commits only part of cleanup, re-adding succeeds, or any non-disposable peer is affected.

## Gate 9 — packaged WSL release and rollback gate

For the supported WSL systemd user-service path, inspect the live help and supply only operator-approved placeholders:

```text
python3 scripts/wsl_release_gate.py --help
python3 scripts/wsl_release_gate.py \
  --version <VERSION> --wheel <WHEEL> --wheel-sha256 <WHEEL_SHA256> \
  --sdist <SDIST> --sdist-sha256 <SDIST_SHA256> \
  --rollback-wheel <ROLLBACK_WHEEL> --venv <VENV> \
  --node-home <HOME_A> --service <SERVICE> \
  --expected-tests <EXPECTED_TESTS> --check-root <CHECK_ROOT> --report <REPORT> \
  --dry-run
```

The script rejects unsafe sdist paths and links, verifies pinned hashes, runs the full suite and Ruff in an isolated environment, checks CLI version/commands, snapshots protected runtime state, validates private permissions, restarts on deployment, and attempts rollback after a deployment-stage failure (`scripts/wsl_release_gate.py:251-431`). Its detailed report is atomically written with private permissions; stdout intentionally exposes only a minimal summary.

**Expected evidence:** dry run reports `dry-run-passed`; an approved deployment reports `deployed`; installed distribution and module versions agree; import resolves inside the persistent environment; service is active; protected state and prekey generations are unchanged; sensitive files remain private; and `pending`, `rejections`, and `untrusted` gates are zero when that is the declared baseline.

**Stop closed:** stop on artifact hash mismatch, unsafe archive content, unexpected test count, static/test failure, version/import mismatch, permission regression, protected-state change, nonzero runtime gate, restart failure, or failed rollback. Never bypass the script by installing directly after a failed gate.

## Gate 10 — cross-device physical LAN validation

This is a separate acceptance gate requiring **two distinct physical devices**, each with its own private home, cryptographic identity, and listener port. Configure Cards with the actual LAN-reachable addresses, verify reciprocal trust out of band, and run direct probes and a benchmark in both directions. Repeat after restarting each device, then perform one controlled carrier-fallback exercise if fallback is in scope.

> `127.0.0.1` proves only same-host reachability. It never proves physical-device LAN reachability.

Windows/WSL on one computer, network namespaces, virtual machines, containers, local proxies, and shared loopback tests remain useful lower-level evidence, but none satisfy this gate. `VERIFICATION.md` explicitly distinguishes its same-machine Windows/WSL results from uncompleted physical-device and real-network validation (`VERIFICATION.md:71-80,568-570,626`).

**Expected evidence:** two independently observed physical-device identifiers kept private; distinct Node IDs and homes; actual listener ownership on each device; sanitized reciprocal Card/PeerBook approval; bidirectional `direct/acked` probes with destination receipts; benchmark success at the predeclared threshold; and successful post-restart repetition. Record the network context generically without publishing addresses or device/service names.

**Stop closed:** do not claim physical-LAN validation if either endpoint is loopback, both runtimes share one physical host, an address was guessed, only one direction succeeds, a carrier masks direct failure, identity was inferred from IP, the destination receipt is absent, or evidence contains undisclosed identity/home/address collisions.

## Final claim discipline

Report only the strongest gate actually completed. Automated tests prove implementation behavior under their fixtures; `doctor` proves local consistency; loopback probes prove same-host delivery; carrier tests prove the configured adapter path; and two-device LAN evidence proves only that tested LAN context. None alone proves cross-operator reachability, censorship resistance, DPI survival, or global unlinkability. Preserve failed runs alongside successful ones so recovery and threshold decisions remain auditable.
