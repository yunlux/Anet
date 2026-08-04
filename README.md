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

## One-command deployment

For a new Windows device, the recommended path is one PowerShell command. It
downloads the deployment entry point, creates one private node, installs the
runtime, registers the auto-start task, and starts the supervisor plus its
anet serve child. The supervisor then polls the control page and can apply
software, default configuration, Peer Cards, and nested pages/kv sources.

The control page must provide software.version and either an initial
software.wheel_url or a repository source in software.repo_url (the top-level
repo_url is also accepted). A wheel plus software.sha256 is the reproducible
package path. With only repo_url, the installer passes the repository to pip as
a Git source, so Git must be available on the device. The supervisor uses that
source for later runtime updates. Set optional software.repo_ref (or top-level
repo_ref) to pin a Git branch, tag, or commit for the initial runtime and later
source updates. The checkout-free bootstrap fetches executable helper scripts
from the official Anet repository and its selected ref by default; it does not
execute a repository chosen by an unverified control page. To use a fork for
the helper scripts, pass `--repository <HTTPS_GITHUB_REPOSITORY>` and
`--script-ref <branch-or-tag>` explicitly. See
docs/windows-control-page.example.json and docs/WINDOWS_AUTOSTART.md.

When the page is signed and a publisher key is pinned, `software.sha256` is
required for every wheel bootstrap/update; a signed `repo_url` remains the
explicit source-install alternative. Unsigned compatibility mode may compute a
local hash, but should only be used with an explicitly trusted page. If an
explicit `--wheel-sha256`/`-WheelSha256` is supplied alongside a declaration,
it must match the signed `software.sha256` value.

For a public or community-maintained page, also pin the publisher in the
deployment command with `-ControlKeyId`/`-ControlPublicKey` on Windows or
`--control-key-id`/`--control-public-key` on POSIX/Termux. The supervisor then
requires signed root and nested pages/KV sources, checks expiry, and rejects
signed sequence reuse with different content. The key is public; keep the
publisher identity offline. Omitting it retains the unsigned compatibility
mode and is only appropriate for an explicitly trusted bootstrap source.

Run this in an ordinary PowerShell window for a current-user installation:

~~~powershell
& ([scriptblock]::Create((Invoke-RestMethod https://raw.githubusercontent.com/yunlux/Anet/main/scripts/install_windows_oneclick.ps1))) `
  -ControlUrl https://example.invalid/anet/control.json `
  -ControlKeyId community-main `
  -ControlPublicKey <BASE64URL_ED25519_PUBLIC_KEY>
~~~

For a machine-wide installation that starts at Windows boot, open PowerShell
with Run as administrator and add -Admin:

~~~powershell
& ([scriptblock]::Create((Invoke-RestMethod https://raw.githubusercontent.com/yunlux/Anet/main/scripts/install_windows_oneclick.ps1))) `
  -Admin `
  -ControlUrl https://example.invalid/anet/control.json `
  -ControlKeyId community-main `
  -ControlPublicKey <BASE64URL_ED25519_PUBLIC_KEY>
~~~

Current-user mode uses %LOCALAPPDATA%/Anet and an AtLogOn task. Administrator
mode uses %ProgramData%/Anet, the SYSTEM principal, and an AtStartup task.
Both modes run a bounded duplicate preflight first; a known existing Anet/Ahub
runtime, service, task, or process stops the install instead of silently
creating a second deployment. Use -AllowExisting only when a second explicit
deployment is intended. A target-scoped install lock is acquired before the
preflight, so concurrent commands cannot both pass the check and create the
same runtime or service.

The same deployment model is available on the other platforms. On a new
device, use the checkout-free POSIX bootstrap; it downloads only the selected
platform entry point and its shared installer modules into a temporary
directory, then removes them after the deployment command exits:

~~~bash
# WSL
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform wsl --control-url <CONTROL_URL> \
  --control-key-id community-main --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>

# non-WSL Linux
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform linux --control-url <CONTROL_URL> \
  --control-key-id community-main --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>

# macOS
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform macos --control-url <CONTROL_URL> \
  --control-key-id community-main --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>

# Android Termux
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform termux --control-url <CONTROL_URL> \
  --control-key-id community-main --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>
~~~

If an Anet checkout is already present, the equivalent direct entry points
remain available under `scripts/install_*_oneclick.py`. Use
`--repository <HTTPS_GITHUB_REPOSITORY>` and `--script-ref <branch-or-tag>`
with the bootstrap when the helper scripts must come from a fork or non-`main`
Git ref.

WSL and Linux create and start a systemd --user supervisor, macOS loads a
LaunchAgent, and Termux uses termux-services/runit plus Termux:Boot. WSL also
needs the Windows host keepalive task if the distribution must start after a
Windows reboot:

~~~powershell
.\scripts\register_wsl_keepalive.ps1 -Distribution Ubuntu -LinuxUser <LINUX_USER>
~~~

Windows and WSL are separate nodes even in mirrored networking. Give them
distinct listener ports and the same opaque host:<zone> context. Port numbers
only isolate listeners; they do not make the two runtimes' 127.0.0.1 addresses
interchangeable. Host-scoped locators must use a shared non-loopback address
(or 0.0.0.0 with explicit -Advertise/--advertise).

Each one-click installer performs a read-only `anet control-verify` after the
initial runtime/node are created and before registering its persistent service.
This verifies the signed root/nested pages and local network/Card policy without
marking the first software update as already applied.

On success, every persistent installer prints one versioned
`anet.deployment.receipt` JSON object with the runtime, independent node,
verified control page, native supervisor state, autostart flag, and preflight
report. The installer also waits for a fresh `supervisor.health` observation
that proves both the supervisor and its `anet serve` child are alive after the
first persistent sync. Platform differences stay inside the `supervisor` object. The receipt
contains private deployment metadata; keep it local and see
[`docs/DEPLOYMENT_RECEIPT_V1.md`](docs/DEPLOYMENT_RECEIPT_V1.md) for the exact
interface and claim limits.

The original runtime-only installers remain available for operators who do not
want a persistent node or service:

~~~text
Windows  scripts/install_windows.ps1
WSL      scripts/install_wsl.py
macOS    scripts/install_macos.py
Linux    skills/install-anet/scripts/install.py
~~~

For the full control-page format, service paths, diagnostics, and CLI/MCP
boundaries, see docs/CLI_AGENT_GUIDE.md, docs/MCP_AGENT_GUIDE.md,
docs/WINDOWS_AUTOSTART.md, docs/POSIX_AUTOSTART.md, and
docs/TERMUX_AUTOSTART.md.

## Agent-assisted installation

If Codex or another coding agent should run the deployment, give it the
following instruction. It should use the one-click entry point above, report
the final node ID, node home, service/task state, and control-page URL, and
never copy identity, TLS private keys, SQLite state, or an entire node home
from another device:

~~~text
Install and deploy Anet from https://github.com/yunlux/Anet. Use this control
page URL: <CONTROL_URL> (replace the placeholder before running). The page
must provide software.version plus software.wheel_url or repo_url; when using
repo_url, an optional repo_ref can pin a branch, tag, or commit. An unverified
control page cannot select executable bootstrap helpers: checkout-free
bootstrap uses the official Anet repository/ref unless `--repository` and
`--script-ref` are explicitly supplied. The page's repo_url remains the
runtime source for the initial install and later source updates; the installer
still requires read-only `control-verify` before registering a persistent
service. This request
also supplies the control publisher pin when the page is signed:
`<CONTROL_KEY_ID>` and `<BASE64URL_ED25519_PUBLIC_KEY>`; pass them through the
platform installer flags, require software.sha256 for signed wheel bootstrap or
updates, and stop if the signature or expiry is invalid.
Before registering the persistent service, require the installer to complete
the read-only `anet control-verify`; it must not consume the first sync state.
This request authorizes one independent persistent node, its service/autostart integration,
and the supervisor's remote control-page polling. Detect the platform and use
its one-click deployment entry point; on native Windows use the PowerShell
one-command entry, adding -Admin only for machine-wide startup. On WSL, Linux,
macOS, or Termux use the checkout-free `bootstrap_posix.py` pipe with the
matching `--platform` value when no checkout is available; use the direct
script when a checkout is already present. If WSL must restart after a Windows
reboot, also register the host keepalive task when that host-side action is
authorized. Treat Windows and WSL as separate nodes:
use separate homes, identities, Node IDs, and listener ports, and never publish
127.0.0.1 as a host-scoped peer address. Report the installed runtime,
independent node, auto-start service/task, node ID, node home, and control-page
URL. Acquire the target-scoped install lock before the bounded duplicate
preflight. Stop on an existing deployment, identity, hash, permission, or
control-page conflict; use -AllowExisting/--allow-existing only when a second
deployment is explicitly requested. Do not copy identity, TLS private keys,
SQLite state, or an entire node home from another device.
Parse the final stdout object only when `kind` is `anet.deployment.receipt`,
`schema_version` is `1`, `ok` is true, `control.verified` is true, and
`supervisor.autostart` is true, `supervisor.health.ok` is true, and both health
process-alive fields are true. Treat the complete receipt as private evidence;
redact the Node ID, paths, addresses, control URL, and service details before
sharing it.
~~~

This prompt only delegates the commands above to an agent; it does not change
the platform-specific node-home, identity-isolation, or duplicate-preflight
rules.

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
