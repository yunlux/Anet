---
type: Quickstart
title: "Anet repository quickstart"
description: "Practical onboarding for Anet v0.9.0: install the CLI, create isolated node runtimes, exchange signed public cards, pair peers, send encrypted messages, and diagnose authenticated direct paths without business traffic."
tags: [anet, quickstart, cli, security, networking]
resource: "README.md"
---

# Anet repository quickstart

Anet is a private encrypted store-and-forward fabric for agent and human edge nodes. Nodes explicitly exchange and pin signed Peer Cards; messages can travel directly, through configured carriers, or in offline bundles. This page is the entrypoint for the repository wiki. The [architecture overview](architecture/overview.md) explains how identity, node homes, packets, carriers, prekeys, acknowledgements, and routing fit together; [onboarding and recovery](operations/onboarding-and-recovery.md) provides the operational lifecycle; the [stdio MCP adapter](integrations/mcp.md) documents the profile, capability, and lifecycle boundary for agent integrations; and the [staged verification gates](testing/verification.md) define fail-closed evidence for tests, identity isolation, delivery, recovery, revocation, release deployment, and physical-device LAN validation.

## Start here

- [README.md](../README.md) is the authoritative hands-on command reference.
- [CLI implementation](../src/anet/cli.py) defines the `anet` command names and options.
- [Configuration](../src/anet/config.py) defines the node-home files and persisted settings.
- [Identity and public cards](../src/anet/identity.py) define the cryptographic Node ID and signed card format.
- [Architecture overview](architecture/overview.md) connects identity, trust, transport, storage, prekeys, acknowledgements, and routing.
- [Node C handoff](../docs/PHYSICAL_NODE_HANDOFF.md) records the physical-device and WSL onboarding boundaries.
- [Onboarding and recovery](operations/onboarding-and-recovery.md) gives the command-oriented lifecycle for homes, cards, pairing, verification, revocation, backups, and carrier outages.
- [Anet stdio MCP adapter](integrations/mcp.md) explains the one-home-per-process binding, capability scope, durable consumer flow, and persistent-versus-ephemeral worker boundary.
- [Observer-local relations](../docs/RELATIONS_V1.md) defines verified Actors, revisable Subject hypotheses, social circles, contextual trust, and their strict separation from authorization.
- [Staged verification gates](testing/verification.md) define required evidence and fail-closed stops from static checks through physical-device LAN validation.

## Install and inspect the CLI

From the repository root, install the editable package and verify the command surface:

```powershell
python -m pip install -e .
anet --help
```

Without installing the package:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m anet --help
```

The default home is `ANET_HOME` when set, otherwise `~/.anet` (`src/anet/cli.py:41-43`). Prefer an explicit `--home` for demonstrations and automation so the runtime boundary is visible.

## Runtime and identity safety

Each persistent agent/runtime owns **one private `ANET_HOME` and one cryptographic identity**. Do not make two persistent agents share or borrow the same node home or identity. The node home contains private or stateful material including `identity.json`, the TLS private key, the SQLite database, configuration, peer state, and revocations (`src/anet/config.py:357-375`; `scripts/wsl_release_gate.py:150-180`). The **identity.json, TLS private key, database, and node home must never be shared or borrowed**.

`card.json` is different: it is a signed public Peer Card produced by `init` or `card`. A public signed `card.json` may be copied through an untrusted or offline channel for review and explicit `peer-add`; copying it does not copy the node identity. Never substitute a copied card for the private identity or state.

## Create two isolated local nodes

The README’s minimum demonstration uses two separate homes and separate ports:

```powershell
anet --home .\demo\a init --label node_a --host 127.0.0.1 --port 43101
anet --home .\demo\b init --label ahub --host 127.0.0.1 --port 43102

anet --home .\demo\a card --out .\demo\a.card.json
anet --home .\demo\b card --out .\demo\b.card.json

anet --home .\demo\a peer-add .\demo\b.card.json
anet --home .\demo\b peer-add .\demo\a.card.json
```

`init` creates the identity, TLS material, peer store, and configuration under the selected home (`src/anet/cli.py:69-90`; `src/anet/config.py:502-525`). `card` exports the signed public card; `peer-add` verifies and pins it (`src/anet/cli.py:93-120`).

### Addressing warning: loopback is not LAN

`127.0.0.1` is same-host loopback only. It is useful for the two-node local demo, but it is **not a Mac LAN address** and must not be advertised as the address of a physically separate Mac. For a physical device, initialize with an unused LAN listening port and advertise the actual reachable LAN address; the Node C handoff explicitly forbids inferring device identity from an IP alone (`docs/PHYSICAL_NODE_HANDOFF.md:19-32`).

For WSL2 mirrored Windows and Linux endpoints, use **distinct ports and distinct cryptographic Node IDs**, even when the endpoints appear on the same host. Mirroring network interfaces does not make two persistent runtimes one identity; each endpoint needs its own private home and identity.

## Safer explicit pairing

When cards are transferred asynchronously, use the signed challenge-response flow rather than independently importing cards:

```powershell
anet --home .\demo\a pair-offer --out .\demo\a.offer.json --ttl 3600
anet --home .\demo\b pair-accept .\demo\a.offer.json --out .\demo\b.response.json
anet --home .\demo\a pair-complete .\demo\a.offer.json .\demo\b.response.json
```

The offer defaults to one hour, and the response is bound to the original offer. `pair-accept` explicitly trusts the offer locally; `pair-complete` verifies the response against the original local offer before pinning the peer (`src/anet/cli.py:222-250`, `src/anet/cli.py:1113-1141`).

## Run, send, and receive

Start one service per private node home:

```powershell
anet --home .\demo\a serve
anet --home .\demo\b serve
```

In another shell, inspect B’s Node ID and queue an encrypted message:

```powershell
anet --home .\demo\b status
anet --home .\demo\a send <B_NODE_ID> --kind message --text "hello"
anet --home .\demo\b inbox --trusted-only
```

`send` writes ciphertext to the local durable queue; `serve` or `sync` performs delivery, so the destination does not need to be online at queue time (`README.md:121-130`). `inbox --trusted-only` reads only trusted local messages.

## Local revocation

If a peer device is lost or its key is suspected to be exposed, revoke the complete Node ID locally:

```powershell
anet --home .\demo\a peer-revoke <PEER_NODE_ID> --confirm <PEER_NODE_ID> --reason "device lost"
anet --home .\demo\a peer-revocations
```

The CLI requires an exact confirmation, records a fail-closed local revocation, and cleans peer-scoped queued work and key state without requiring a restart (`src/anet/cli.py:128-155`). This is local trust revocation, not a network-wide broadcast; use a new device identity to establish a replacement relationship.

## Optional direct SOCKS5 proxy

The v0.5.2 direct-link proxy changes only the TCP dial path; TLS 1.3, signed peer handshakes, Node IDs, cards, encryption, trust, and revocation semantics remain unchanged:

```powershell
anet --home .\demo\a direct-proxy socks5h://127.0.0.1:1080
anet --home .\demo\a direct-proxy
anet --home .\demo\a direct-proxy --clear
```

Only loopback proxies are allowed by default. Remote proxies require `--allow-remote`; credentials are supplied by paired environment-variable names, never embedded in the URL or printed (`src/anet/config.py:32-94`; `src/anet/cli.py:589-621`).

## WSL release gate

For a WSL systemd user-service deployment, inspect the fixed release gate before using it:

```bash
python3 scripts/wsl_release_gate.py --help
```

The gate accepts pinned package hashes, a rollback wheel, a virtual environment, a node home, and a service; it tests, installs, restarts, compares protected identity/state files, and can roll back. Its detailed evidence is written locally with restricted permissions while stdout is intentionally compact (`README.md:61-67`; `scripts/wsl_release_gate.py:80-99`, `150-180`).
