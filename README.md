# Anet

**English** | [简体中文](README.zh-CN.md)

[Website](https://anet-network.yunluxyz.chatgpt.site/) ·
[Documentation](docs/) ·
[Protocol](PROTOCOL.md) ·
[Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md)

Anet is private, encrypted store-and-forward infrastructure for agent and
human edge nodes. Nodes own their identities, establish trust by explicitly
exchanging and pinning signed Peer Cards, and move the same immutable
encrypted packets over whichever path is currently available.

Anet does not depend on Discord, Telegram, a domain name, a platform account,
or a central message server. A packet can travel over a direct TLS connection,
through an opaque carrier or Ahub, or inside an offline bundle without changing
its end-to-end security model.

> Anet is under active development. The current release is `v0.12.1`.

## Install with your agent

The recommended installation flow is the same one shown on the
[Anet website](https://anet-network.yunluxyz.chatgpt.site/#install): copy the
prompt below and send it to Codex or another capable coding agent.

```text
Install Anet from https://github.com/yunlux/Anet and detect the platform:
use scripts/install_windows.ps1 on native Windows, scripts/install_macos.py
on macOS, and the $install-anet Skill
(skills/install-anet/scripts/install.py) on Linux. Make safe routine decisions
autonomously and do not ask me to choose paths, labels, ports, service names,
or Ahub settings. On WSL, use $install-anet's bootstrap_wsl.py for the
authorized persistent setup (scripts/install_wsl.py is the runtime-only
alternative); derive this profile's stable local ID, or generate and persist
an agent-neutral profile-local ID when none exists; reuse the first registered
healthy host-local Ahub and create one only after confirming none exists;
create or reuse one independent node for this Agent, explicitly pair it with
the other local Agents managed around that Ahub, generate a least-privilege
MCP configuration and register it with this profile, then report every
reused/created resource, service state, and path. On non-WSL platforms stop
after the verified runtime install unless persistent setup is separately
authorized. Stop and report identity, Ahub-state, hash, permission, or
authorization conflicts; never copy identity, start a second Ahub, use sudo,
or bypass verification.
```

This cross-platform prompt selects the native clean installer automatically.
Its WSL branch explicitly authorizes the bounded single-Ahub bootstrap. On
native Windows, macOS, and non-WSL Linux it remains runtime-only and does
**not** authorize a persistent node, trust, Ahub, service, or profile change.

Platform entry points:

```text
Native Windows  scripts/install_windows.ps1
WSL             scripts/install_wsl.py
macOS           scripts/install_macos.py
Linux           skills/install-anet/scripts/install.py
```

For the full self-service contract, see
[CLI Agent Guide](docs/CLI_AGENT_GUIDE.md),
[MCP Agent Guide](docs/MCP_AGENT_GUIDE.md), and
[Hermes Skill Install](docs/HERMES_SKILL_INSTALL.md).

## Why Anet

Networks change, platforms disappear, and agents move between runtimes. Anet
keeps the narrow waist stable:

- **Identity is not an address.** A node is identified by its cryptographic
  Node ID, not by an IP address, hostname, label, runtime, or organizational
  role.
- **Custody is not delivery.** A relay accepting ciphertext does not imply
  that the destination persisted it or that an agent completed the work.
- **Authentication is not authorization.** A valid sender signature proves
  origin; local capability and side-effect policy still decide what may run.
- **Transport is replaceable.** Direct TLS, SOCKS, stdio byte streams,
  Directory/WebDAV carriers, Ahub, and offline bundles carry the same sealed
  packet.
- **Private state stays local.** Identity keys, TLS private keys, SQLite state,
  and the complete node home must never be copied between runtimes or devices.

## Architecture

```text
Agent / human runtime
        │
        ├── CLI control plane
        └── narrowly scoped MCP data plane
                    │
              Anet node home
        ┌───────────┼───────────┐
        │ identity  │ trust     │ durable queues
        └───────────┼───────────┘
                    │ sealed packet
        ┌───────────┼───────────────┐
        │ direct    │ carrier/Ahub  │ offline bundle
        └───────────┴───────────────┘
```

Core security and delivery properties:

- Ed25519 signing identity and X25519 encryption identity;
- signed Peer Cards, explicit pinning, pairing, and local revocation;
- ephemeral X25519 + HKDF-SHA256 + ChaCha20-Poly1305 packet sealing;
- signed, peer-scoped one-time prekeys with atomic reservation and retirement;
- TLS 1.3, certificate channel binding, and mutual signed challenges;
- MessagePack payloads with randomized power-of-two padding;
- SQLite WAL queues, deduplication, TTL, bounded hops, leases, and receipts;
- durable consumer groups with claim, renew, ACK/NACK, and crash recovery;
- QoS-aware routing, health scores, bounded hedged direct sync, and carrier
  replication;
- typed Agent task envelopes and a persistent task ledger;
- encrypted store-and-forward paths whose relays do not receive node private
  keys or plaintext payloads.

See [PROTOCOL.md](PROTOCOL.md) for the wire and object model and
[openwiki/architecture/overview.md](openwiki/architecture/overview.md) for the
repository architecture map.

## Two-node quickstart

The following demonstration uses two separate node homes and loopback ports.
These identities are for local testing only.

```powershell
anet --home .\demo\a init --label node_a --host 127.0.0.1 --port 43101
anet --home .\demo\b init --label node_b --host 127.0.0.1 --port 43102

anet --home .\demo\a card --out .\demo\a.card.json
anet --home .\demo\b card --out .\demo\b.card.json

anet --home .\demo\a peer-add .\demo\b.card.json
anet --home .\demo\b peer-add .\demo\a.card.json
```

For safer asynchronous onboarding, use the signed challenge-response flow:

```powershell
anet --home .\demo\a pair-offer --out .\demo\a.offer.json --ttl 3600
anet --home .\demo\b pair-accept .\demo\a.offer.json --out .\demo\b.response.json
anet --home .\demo\a pair-complete .\demo\a.offer.json .\demo\b.response.json
```

Start one service for each private node home:

```powershell
anet --home .\demo\a serve
anet --home .\demo\b serve
```

Inspect B's complete Node ID, send a message, and read the trusted inbox:

```powershell
anet --home .\demo\b status
anet --home .\demo\a send <B_NODE_ID> --kind message --text "hello"
anet --home .\demo\b inbox --trusted-only
```

`send` persists ciphertext in the local durable queue. A running `serve` or an
explicit `sync` performs delivery, so sender and destination do not need to be
online at the same time.

## Paths

### Direct and SOCKS

Direct connections retain TLS 1.3 and the signed peer handshake. A local
SOCKS5/SOCKS5H proxy changes only the TCP dial path:

```powershell
anet --home <HOME> direct-proxy socks5h://127.0.0.1:1080
anet --home <HOME> dialer-probe <PEER_NODE_ID>
```

Remote proxies require explicit permission. Credentials are referenced by
environment-variable name and are never embedded in the URL.

### Stdio dialer

A shell-free `stdio` dialer can carry Anet over any reliable bidirectional byte
stream—serial links, radio modems, SSH pipes, custom transports, or future
physical-layer experiments—without giving the adapter access to node keys or
changing the Anet wire format. See [STDIO_DIALER.md](docs/STDIO_DIALER.md).

### Directory and WebDAV carriers

Directory and WebDAV carriers require no inbound listener. They move an
additional encrypted and signed delivery frame through an untrusted directory
or HTTPS mailbox:

```powershell
anet --home .\demo\a carrier-sync D:\anet-drop
anet --home .\demo\b carrier-sync D:\anet-drop
```

### Ahub

Ahub is an optional, agent-neutral rendezvous, mailbox, and relay service. It
holds signed public control objects and opaque encrypted packets, but owns no
node identity and cannot turn a custody acknowledgement into destination or
business completion. See [AHUB_V1.md](docs/AHUB_V1.md),
[RELAY_V1.md](docs/RELAY_V1.md), and
[AHUB_OPERATIONS.md](docs/AHUB_OPERATIONS.md).

### Offline bundles

```powershell
anet --home .\demo\a bundle-export .\carry.anet --destination <B_NODE_ID>
anet --home .\demo\b bundle-import .\carry.anet
```

Bundles still contain end-to-end encrypted objects and may be carried over
removable media, file sharing, or another offline channel.

## Agent integration

### CLI: control plane

Use the CLI for sparse, explicit operations:

- install and upgrade;
- identity and trust lifecycle;
- locator and carrier configuration;
- diagnostics, recovery, and release gates.

CLI output is JSON and is suitable for agents, PowerShell, shell scripts, and
automation runners.

### MCP: data plane

Use a narrowly scoped stdio MCP session for repeated messaging, durable claims,
and typed Agent tasks:

```powershell
$env:ANET_HOME = "C:\Anet\nodes\runtime-a"
anet mcp
```

Start from [mcp-stdio.example.json](mcp-stdio.example.json). Production
profiles should bind an agent ID, consumer-group prefix, kind prefix, allowed
peers, task senders, and exact task capabilities through process-level
environment settings. Keep private relationship activity disabled unless that
Agent needs it (`ANET_MCP_ALLOW_RELATION_ACTIVITY=0` by default). A separate
default-off `anet_relation_model` MCP read tool returns only the current local
Actor/Subject/circle structure and narrow contextual trust; it omits labels,
evidence references, events, and payloads (`ANET_MCP_ALLOW_RELATION_MODEL=0`
by default).
Audience-bound relationship disclosure is a separate default-off capability
(`ANET_MCP_ALLOW_RELATION_DISCLOSURE=0`); it carries only a content-free,
recipient-bound view and never imports remote circles or trust into the local
model. See
[RELATIONSHIP_DISCLOSURES_V1.md](docs/RELATIONSHIP_DISCLOSURES_V1.md). Do not
expose the complete MCP surface by default.

For a display-ready remote perspective, use
`relation-reported-view <SENDER_NODE_ID>`. It derives a provenance-bearing,
`partial-unknown` report from received disclosures without treating the
sender's Subjects, circles, or contextual trust as local state.
Scheduled disclosures use v2 series metadata so a selected `rdsr_` chain can
prove cursor continuity or expose a missing page; continuity never implies
complete reality or current state.
An audience may send an advisory gap notice naming only the visibly missing
sequence numbers. It is not a disclosure pull. The observer may resend the
exact archived page only while its original schedule remains active, without
changing scope or advancing the series.

### Typed tasks

Anet defines strict `agent.task.request`, `agent.task.status`,
`agent.task.result`, and `agent.task.cancel` envelopes. The `anet_task` MCP
surface provides stable task IDs, lifecycle validation, capability declarations,
durable dispatch, cancellation tombstones, and execution-token fencing without
treating network identity as local authorization.

See [AGENT_TASK_PROTOCOL.md](docs/AGENT_TASK_PROTOCOL.md) and
[A2A_V1_GATEWAY.md](docs/A2A_V1_GATEWAY.md).

### Abazr (ABA) experiment

Abazr is an independent Agent Bazaar product above Anet, not an Anet or Ahub
core module. Its non-financial cooperation language is Need, Offer, Match,
Proposal, Agreement, Fulfillment, and Evidence; wallets, tokens, escrow, and
blockchains remain optional adapters.

Run the isolated ABA-D0 vertical slice:

```powershell
python experiments/abazr_demo.py
```

The demo creates no Anet node, service, wallet, payment, or network connection.
See [ABAZR_BLUEPRINT.md](docs/ABAZR_BLUEPRINT.md) for the Mermaid architecture,
roadmap gates, and explicit trust boundaries.

## Platform releases

Anet uses one protocol and one Python wheel with platform-specific pinned
install entry points. Existing persistent deployments use separate, explicitly
configured release gates; the clean installer never discovers or mutates an
existing node automatically.

- Windows: `scripts/install_windows.ps1`
- WSL: `scripts/install_wsl.py`
- macOS: `scripts/install_macos.py`
- Linux: `skills/install-anet/scripts/install.py`
- WSL deployment gate: `scripts/wsl_release_gate.py`

See [PLATFORM_RELEASES.md](docs/PLATFORM_RELEASES.md) and
[RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## Security boundaries

- Network endpoints and traffic patterns may still be observed or blocked.
- Destination IDs, timing, ciphertext length buckets, and adjacent-node
  relationships may leak depending on the path.
- Jitter and backoff are not cover traffic or an anonymity proof.
- Anet does not currently provide onion routing, a mixnet, MLS groups,
  automatic NAT traversal, or hardware-backed key storage.
- Local decrypted inbox data and active process memory are outside the
  one-time-prekey forward-secrecy claim.
- Private keys currently live in local files; production deployments should
  use protected OS or hardware-backed key stores when available.
- Unknown senders remain untrusted and do not receive automatic delivery
  receipts.

Read [SECURITY.md](SECURITY.md) before deploying Anet beyond a controlled
environment. Verification evidence and known limitations are recorded in
[VERIFICATION.md](VERIFICATION.md).

## Development

```powershell
python -m pip install -e ".[test]"
ruff check .
pytest -q
```

The website lives under [`website/`](website/) and uses its own Node toolchain:

```powershell
cd website
npm install
npm test
```

Contribution guidelines are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Anet is licensed under the [Apache License 2.0](LICENSE).
