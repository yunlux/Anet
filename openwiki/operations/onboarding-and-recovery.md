---
type: Operations Runbook
title: "Anet onboarding and recovery"
description: "Command-oriented lifecycle for creating isolated Anet node homes, validating and pinning signed Peer Cards, verifying service health, handling revocation and outage carriers, and recovering without sharing private identity state."
tags: [anet, operations, onboarding, recovery, security, pairing, wsl, mac]
resource: "README.md"
---

# Anet onboarding and recovery

This runbook covers the operator lifecycle for a node whose durable boundary is its private `ANET_HOME`. It complements the [quickstart](../quickstart.md) and [architecture overview](../architecture/overview.md); the CLI implementation in [`src/anet/cli.py`](../../src/anet/cli.py) is authoritative when examples and installed behavior differ.

## 1. Decide the runtime boundary first

Choose whether the runtime is **persistent** or **ephemeral**:

- A persistent node keeps one private home for its identity, TLS key, configuration, peer trust, revocations, and SQLite queue. Use a stable path and protect it from synchronization and public backup destinations.
- Ephemeral workers do not initialize persistent nodes by default. If a test explicitly needs a node, use a disposable `--home`; Anet has no in-memory or special `ephemeral` mode. Never promote or reuse that temporary home as a persistent identity.

Every independent runtime needs a unique home and a unique port. `ANET_HOME` is the fallback only when `--home` is omitted, so make the boundary explicit in operational commands:

```powershell
$home = "C:\Anet\nodes\agent-a"       # template: choose a private path
$port = 43101                            # template: choose an unused port
anet --home $home init --label agent-a --host 127.0.0.1 --port $port
```

Do not initialize twice in the same home: `init` refuses an existing identity/configuration. The home contains high-value state such as `identity.json`, `tls-key.pem`, `peers.json`, `revocations.json`, `config.json`, `anet.sqlite3`, and possible SQLite WAL files (`src/anet/config.py:357-375`; `scripts/wsl_release_gate.py:150-180`).

### Host-specific locator rules

All real addresses, ports, Node IDs, and service-unit names are deployment-owned values. The examples below are templates, not deployment values.

- **WSL2 mirrored Windows/WSL runtimes:** use separate private homes, cryptographic identities, and listening ports even when both endpoints share a host or mirrored interfaces. Mirroring does not merge runtimes. The release-gate evidence also treats identity, peers, revocations, configuration, TLS material, and database state as protected files (`scripts/wsl_release_gate.py:150-180`; `tests/test_wsl_release_gate.py`).
- **Scoped locators:** give Windows and its same-host WSL runtime the same opaque `host:<HOST_ZONE>` context, but keep distinct ports and Node IDs. Give all physical devices on the intended LAN an opaque `lan:<LAN_ZONE>` context. A physical Mac shares the LAN zone but never the Windows/WSL host zone. Configure and re-sign atomically:

  ```powershell
  anet --home <HOME> locator-config --add-context host:<HOST_ZONE> --add-context lan:<LAN_ZONE> `
    --advertise "tls://127.0.0.1:<PORT>?scope=host&zone=<HOST_ZONE>&priority=0" `
    --advertise "tls://<LAN-IP>:<PORT>?scope=lan&zone=<LAN_ZONE>&priority=20"
  anet --home <HOME> doctor
  ```

  Zones are signed routing hints, not secrets or identity. Use random opaque values rather than host/location names. A locator is attempted only when its host/LAN context matches; legacy unscoped addresses remain globally attemptable for rolling compatibility.
- **Physical Mac:** supply the actual reachable LAN IP and an unused LAN port; do not advertise `127.0.0.1`, a guessed address, or a public address. The repository bootstrap is a template requiring an explicit LAN locator and prepares but does not start the node:

  ```bash
  ./scripts/bootstrap-macos.sh <anet-wheel> --sha256 <VERIFIED-SHA256> \
    --advertise <LAN-IP> --lan-zone <LAN_ZONE> --port 4246 --label <label>
  ```

  It initializes with `0.0.0.0`, advertises a LAN locator, exports a keys-only card, and does not add peers or start `serve` (`scripts/bootstrap-macos.sh`; `docs/PHYSICAL_NODE_HANDOFF.md`). Before starting it, add the shared LAN context with `locator-config`, re-export the signed address-bearing card, and confirm the Node ID through a second authenticated channel; an IP address is not identity.

### Configure adaptive direct dialers

Anet v0.7 treats raw TCP and each SOCKS5/SOCKS5H route as independently measured paths. Keep raw at the lower priority when it is normally available and add one or more proxy fallbacks. Credentials are referenced by environment-variable name; never put them in the URL or Peer Card:

```powershell
anet --home <HOME> dialer-add mihomo --type socks5h `
  --url socks5h://127.0.0.1:7890 --priority 20 `
  --username-env ANET_PROXY_USER --password-env ANET_PROXY_PASS
anet --home <HOME> dialer-list
```

An old node with no `direct_proxy` behaves as one raw dialer; an old node with `direct_proxy` behaves as one legacy proxy dialer. The first `dialer-add` or `dialer-set` materializes that effective behavior as explicit configuration. Use `dialer-set` for reversible tests, restart the service, probe, then restore the path:

```powershell
anet --home <HOME> dialer-set raw --no-enabled
# restart the owning service, then probe and inspect status/path metrics
anet --home <HOME> dialer-set raw --enabled
# restart again and confirm raw recovery
```

`dialer-remove` removes explicit configuration. If no explicit dialer remains, compatibility fallback becomes raw again; to disable the entire direct family, use `routing-config --no-direct`. A successful proxy CONNECT is not peer authentication: TLS and the signed Anet handshake must still complete.

For a measured weak-network deployment, enable at most two candidates first and retain a hedge delay so healthy traffic remains single-path:

```powershell
anet --home <HOME> routing-config --direct-race-width 2 --direct-race-delay 0.15
# restart the owning service, then use probe/status to verify the winning fine path
```

Width 1 disables hedging. Width 4 is a hard maximum, not a recommended default: it increases simultaneous connections, bandwidth and correlation surface. A cancelled delayed candidate is intentionally not counted as failed. Test with real RTT/loss/blocks before lowering the delay, and restore width 1 when the extra observable attempts are not justified.

Use the authenticated health probe to separate path establishment failures from business delivery. It performs TLS and Anet identity authentication but does not exchange queued Packet state:

```powershell
anet --home <HOME> dialer-probe <PEER_NODE_ID>
anet --home <HOME> dialer-probe <PEER_NODE_ID> --dialer mihomo --require-all
```

The default exit status is successful when at least one tested path is healthy; `--require-all` requires every selected dialer/locator. Inspect `category`, not just the error string. `health_unsupported` means the peer predates the capability and is not evidence of blocking. Health probes are observable active traffic, so run them on demand or at a measured low frequency rather than as a fixed high-rate heartbeat.

## 2. Initialize once and distribute only public cards

After choosing the home and port, initialize once and export the signed public card:

```powershell
anet --home $home init --label <label> --host <listen-host> --port <unused-port>
anet --home $home card --keys-only --out .\exchange\<label>.card.json
```

Distribute only the resulting signed public card (or a pairing offer/response). Do not distribute `identity.json`, `tls-key.pem`, `config.json`, `peers.json`, `revocations.json`, `anet.sqlite3`, WAL files, or the whole home. `card` derives a public card from the owning private identity; it does not create a replacement identity.

There is no standalone `card-validate` command. Validation is performed when the card is loaded by `peer-add` or the pairing commands: the implementation checks the card version, signature, key consistency, Node ID derivation, and supported TLS address schemes. Compare the complete expected Node ID through an authenticated second channel before trusting it; labels and addresses are locators/metadata, not identity.

## 3. Pair and pin trust explicitly

For a simple signed-card exchange, each side verifies and pins the other card locally:

```powershell
anet --home <A_HOME> peer-add .\exchange\<B_LABEL>.card.json
anet --home <B_HOME> peer-add .\exchange\<A_LABEL>.card.json
anet --home <A_HOME> peer-list
anet --home <B_HOME> peer-list
```

For asynchronous onboarding, prefer the signed challenge flow. These paths are templates:

```powershell
anet --home <A_HOME> pair-offer --out .\exchange\a.offer.json --ttl 3600
anet --home <B_HOME> pair-accept .\exchange\a.offer.json --out .\exchange\b.response.json
anet --home <A_HOME> pair-complete .\exchange\a.offer.json .\exchange\b.response.json
```

`pair-accept` verifies the offer and pins the offer card locally; `pair-complete` verifies the response against the original offer and then pins the response card. Offers are time-bounded (default one hour; implementation bounds the requested TTL), and the response is bound to the offer digest (`src/anet/pairing.py`; `tests/test_pairing.py`). Do not treat receipt of a card, offer, or response as authorization until the local pinning step succeeds.

For a camera/image workflow, the equivalent signed QR friend exchange is:

```powershell
anet --home <A_HOME> friend-qr --out .\exchange\a-friend.png --ttl 600
anet --home <B_HOME> friend-scan .\exchange\a-friend.png --out .\exchange\b-response.png
anet --home <A_HOME> friend-scan .\exchange\b-response.png
anet --home <A_HOME> relation-list
```

The QR contains only a compressed public invite or acceptance. It never carries
private node state. Each explicit scan records the verified peer Actor in the
local `friend` circle, while the concrete Subject behind that Actor remains a
local hypothesis. Circle placement does not grant operational capability. See
[`docs/QR_FRIENDS.md`](../../docs/QR_FRIENDS.md).

## 4. Validate, pair, inspect, doctor, serve, and probe

The operational order is: validate the signed card by pinning or pairing, inspect `peer-list`, run `doctor`, start `serve`, then use `probe`. Start exactly one service for each node home; do not launch two independent services against the same home:

```powershell
anet --home <A_HOME> serve
anet --home <B_HOME> serve
```

In a separate shell, run local checks. Replace placeholders with values obtained from the node’s own output or trusted card; never invent a Node ID:

```powershell
anet --home <HOME> doctor
anet --home <HOME> peer-list
anet --home <HOME> status
anet --home <HOME> send <DESTINATION_NODE_ID> --kind message --text "health-check"
anet --home <HOME> probe <DESTINATION_NODE_ID>
```

`doctor` checks local identity/card, TLS material, peer count, store, prekeys, and locator warnings; it does not prove remote reachability. A legacy loopback warning means physical peers may first try their own loopback and should be corrected with a scoped locator. `peer-list` shows pinned, non-revoked peers. `send` queues encrypted data locally, while `serve`/`sync` performs delivery. `probe` is the active reachability check and returns success or failure by exit status. There is no `listeners` or `check-listener` CLI command: use `serve` plus `status`/`doctor` and host OS service/socket tooling when listener binding itself must be inspected (`src/anet/cli.py`; `tests/test_cli.py`).

## 5. Handle address or capability changes safely

When a node’s advertised address or capabilities change, regenerate the public card **from the owning private identity** and redistribute it through the approved authenticated channel:

```powershell
anet --home <HOME> card --out .\exchange\<label>-updated.card.json
# Or omit addresses when the recipient should receive only key material:
anet --home <HOME> card --keys-only --out .\exchange\<label>-keys.card.json
```

Never hand-edit signed card fields. A changed address must be produced by the node configuration and re-signed by `card`; otherwise signature and Node ID checks fail. Do not run `init` to “regenerate” a card, and do not copy a private identity to another device. If the private identity is unavailable or compromised, create a new home/identity and establish a new trust relationship instead.

## 6. Revoke, quarantine, then back up before repair

If a peer is lost, stale, or suspected compromised, record the complete Node ID and revoke it locally with exact confirmation:

```powershell
anet --home <HOME> peer-revoke <PEER_NODE_ID> --confirm <PEER_NODE_ID> --reason "device lost or compromised"
anet --home <HOME> peer-revocations
anet --home <HOME> peer-list
```

This is the repository’s quarantine mechanism, not a separate `quarantine` command. The deny ledger is written before positive trust is removed; runtime checks reload it, and revocation cleans peer-scoped queued work, inbox trust/claims, routes, metrics, and key state without a restart (`src/anet/peers.py`; `src/anet/cli.py`; `tests/test_revocation.py`). It is local, not network-wide, and cannot undo already executed side effects or other nodes’ trust state. There is no ordinary unrevoke/restore command; a replacement device uses a new identity and a new pairing.

Immediately after quarantine—and **before** repair, package upgrade, migration, or manual intervention—stop the service and make a protected, access-controlled backup/snapshot of the entire node home. Include the database and WAL state, but treat the backup as containing private identity and possibly prekey material; logical deletion does not guarantee media-level erasure (`SECURITY.md:40-45,75-77`). Preserve the original home for rollback rather than experimenting on the only copy.

## 7. Recover without borrowing a home

Anet does not provide `backup`, `restore`, `repair`, `recover`, or `fsck` commands. `scripts/wsl_release_gate.py` can validate/rollback a deployment artifact and service while checking protected node files; it is not a node-data repair tool. Do not recover by borrowing another node’s home. Restore the owning node’s protected home only to the same identity boundary, or initialize a new home and pair it as a replacement identity when that boundary cannot be trusted.

For an outage where direct delivery is unavailable, keep the same identity and sealed packets and use configured carrier fallback rather than copying private state. A configured carrier can run in `fallback` mode, and adaptive routing switches when its configured direct-failure threshold is reached; inspect the configured carriers before forcing a one-off synchronization pass:

```powershell
anet --home <HOME> carrier-list
anet --home <HOME> sync
anet --home <HOME> carrier-sync <CARRIER_ROOT> --peer <DESTINATION_NODE_ID>
# A polling carrier service is an alternative to opening a direct listener:
anet --home <HOME> carrier-serve <CARRIER_ROOT> --peer <DESTINATION_NODE_ID>
```

Carrier options and exact required arguments depend on the configured directory/WebDAV carrier. `sync` performs an adaptive pass; `carrier-sync` and `carrier-serve` provide bounded store-and-forward paths. Carrier fallback moves the same sealed packet and does not change the Node ID, Peer Card, or encryption scheme (`README.md:180-242`; `src/anet/cli.py`; `tests/test_webdav_carrier.py`). After connectivity returns, run `doctor`, `peer-list`, `status`, and a placeholder-based `probe`, then send a test message and confirm receipt through the normal trusted inbox path.

For a WSL systemd user-service release, use the deployment-owned `scripts/wsl_release_gate.py` inputs rather than manually replacing runtime files. The gate verifies artifact hashes, snapshots Node ID, peers, revocations, protected-file hashes and permissions, runs isolated tests, restarts the named service, and installs the supplied rollback wheel if deployment fails (`scripts/wsl_release_gate.py:208-235,370-431`). It protects an existing home; it does not create a backup or repair corrupt node data.

## Operational cautions

- Keep private homes, cards, offers, and responses in separate protected locations; public cards/offers/responses must not contain secrets.
- Do not infer identity from labels, IP addresses, ports, or successful carrier custody.
- Do not hand-edit signed artifacts or reuse a stale card after an address/capability change.
- Treat backup, VM snapshot, WAL, and release artifacts as sensitive; avoid public cloud sync and Git.
- When a command is unavailable, use only the documented nearest mechanism above rather than inventing a CLI verb.
