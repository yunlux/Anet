---
type: MCP Integration
title: "Anet stdio MCP adapter"
description: "Operating and securing Anet's stdio MCP adapter: bind each process to one isolated node home, scope its tools by capability, and separate persistent agent ownership from ephemeral worker execution."
resource: "src/anet/mcp_server.py"
tags: [anet, mcp, integrations, security, agents]
---

# Anet stdio MCP adapter

The `anet mcp` command exposes a local Anet node as a **stdio** MCP server. It is an adapter over the node selected by `ANET_HOME`, not a shared network service: its lifespan loads one `NodeConfig`, creates one `AnetNode`, and closes that node when the stdio session ends ([`src/anet/mcp_server.py`](../../src/anet/mcp_server.py)). The adapter therefore shares the selected home’s identity, pinned peers, revocations, and durable store with the node described in the [architecture overview](../architecture/overview.md), while a separate `anet serve` process remains responsible for inbound network listening.

Install the optional MCP dependency before launching it:

```powershell
python -m pip install -e ".[mcp]"
```

`mcp` is an optional project extra rather than a base dependency ([`pyproject.toml`](../../pyproject.toml)).

## Bind one profile to one home

Set `ANET_HOME` before starting the MCP process. This chooses the single local node the adapter will open; do not change it during a session or point multiple independently managed profiles at the same home.

```powershell
$env:ANET_HOME = "<ISOLATED_ANET_HOME>"
anet doctor
anet mcp
```

`anet doctor` verifies identity, TLS material, trust store, and spool before the process starts ([`src/anet/cli.py`](../../src/anet/cli.py)). When an MCP client starts `anet mcp`, stdin/stdout belong to the MCP protocol: do not use that channel for operator commands or human-readable status. The FastMCP lifespan stops and closes the node when the stdio session ends, so the client should treat process exit, broken stdio, or startup failure as an unavailable local adapter—not as a remote delivery result.

The adapter does not call the network-serving loop. Run the node’s listener separately when direct inbound transport is required:

```powershell
# Persistent node service, managed separately from the MCP client session
anet --home "<ISOLATED_ANET_HOME>" serve

# MCP adapter, with the same explicitly owned home
$env:ANET_HOME = "<ISOLATED_ANET_HOME>"
anet mcp
```

This separation avoids several client profiles competing for a listening port. It also means `anet_send` queues work locally and `anet_sync` requests an outbound synchronization pass; neither makes the MCP process a substitute for a continuously managed listener.

## Persistent agents and ephemeral workers

A persistent agent may own one private node home and run a long-lived MCP adapter against it. Bind that adapter to a fixed identity label with `ANET_AGENT_ID` and scope its consumer namespace. An ephemeral worker is different: it **must not borrow a persistent agent’s identity or node home**, and it receives no Anet MCP server by default.

Allocate an ephemeral worker an adapter only when it has its **own isolated node home, its own capability scope, and an explicit startup/shutdown lifecycle**. Otherwise, pass work through the owning persistent agent’s durable consumer flow rather than giving the worker direct access to that agent’s Anet state. This preserves the private-home boundary documented in the [quickstart](../quickstart.md#runtime-and-identity-safety) and prevents an arbitrary short-lived process from claiming, reading, or sending as a durable identity.

A generic persistent-agent environment is:

```powershell
$env:ANET_HOME = "<PERSISTENT_AGENT_HOME>"
$env:ANET_AGENT_ID = "<PERSISTENT_AGENT_OWNER>"
$env:ANET_MCP_GROUP_PREFIX = "<PERSISTENT_AGENT_NAMESPACE>."
$env:ANET_MCP_KIND_PREFIX = "<PERSISTENT_AGENT_KIND_PREFIX>"
$env:ANET_MCP_ALLOWED_PEERS = "<PEER_ID_1>,<PEER_ID_2>"
$env:ANET_MCP_ALLOW_RAW_INBOX = "0"
anet doctor
anet mcp
```

Use placeholders as shown; do not put private paths, node identifiers, or secrets in profile documentation or command arguments.

## Tool surface and capability boundaries

The server exposes read/status tools (`anet_status`, `anet_peers`, `anet_card`), message and transport tools (`anet_send`, `anet_sync`, `anet_probe`), and local inbox/consumer tools (`anet_inbox`, `anet_consumer_open`, `anet_claim`, `anet_settle`, `anet_claim_renew`, `anet_consumer_status`). Tool results are JSON-safe serializations of local state or operations.

Process environment is the capability boundary; tool arguments cannot expand it:

| Capability variable | Enforced effect |
| --- | --- |
| `ANET_AGENT_ID` | Fixes the owner used for claims. A conflicting `owner` argument is rejected; without either a configured or supplied owner, claiming fails. |
| `ANET_MCP_GROUP_PREFIX` | Allows only durable consumer groups beginning with the configured prefix. |
| `ANET_MCP_KIND_PREFIX` | Forces or limits the consumer kind prefix to the configured namespace. |
| `ANET_MCP_ALLOWED_PEERS` | Allows sends and probes only to the comma-separated peer allowlist (or `*` when intentionally unrestricted). |
| `ANET_MCP_ALLOW_RAW_INBOX=0` | Disables `anet_inbox`, requiring consumers to read through leased claims instead. |
| `ANET_MCP_ALLOW_UNTRUSTED=1` | Explicitly permits consumer groups to include untrusted input; the default rejects that request. |
| `ANET_MCP_ALLOW_TRANSIENT=1` | Explicitly permits consumer groups to include transient input; the default rejects that request. |

The adapter’s tests establish these rejection paths: a caller cannot substitute a different claim owner, escape the configured group or kind namespace, send to a blocked peer, or use raw inbox access once disabled ([`tests/test_mcp.py`](../../tests/test_mcp.py)). Even cryptographically authenticated sender data remains input to validate under local policy before side effects.

For durable work, open the scoped group, claim messages under the fixed owner, acknowledge only after durable completion, and use `nack` or renewal when appropriate. Groups default to `start="latest"`, so a newly created consumer does not silently replay historical inbox entries; its filters become immutable after creation. This workflow uses the node’s durable store and is related to the queue and acknowledgement model in the [architecture overview](../architecture/overview.md).

## Public exchange, private ownership, and trust changes

`anet_card` returns the selected node’s signed **public Peer Card** for out-of-band exchange. Exchanging that card does not transfer ownership of the node home, identity keys, TLS key, local database, or peer state. Importing or accepting a peer remains a trust-boundary action managed outside this MCP tool surface: notably, pairing acceptance is not exposed by the adapter ([`README.md`](../../README.md)).

Treat `anet_card` as public exchange material and keep the `ANET_HOME` contents private. A consumer can inspect pinned peers with `anet_peers`, but it cannot use the adapter to create a broader trust relationship merely by supplying a different tool argument. For revocation, recovery, and card-validation procedures, follow [onboarding and recovery](../operations/onboarding-and-recovery.md).

## Startup, failure handling, and client hygiene

1. Install the `mcp` extra, select an isolated `ANET_HOME`, and run `anet doctor` before starting the adapter.
2. Configure the narrowest feasible owner, consumer, kind, peer, inbox, and untrusted/transient capabilities in the process environment.
3. Start `anet mcp` as a stdio child of the intended profile; keep its stdout protocol-only.
4. Ensure every other MCP server enabled in that profile can start successfully. A client may initialize all enabled servers, so an unrelated failure can prevent the Anet session from remaining available ([`README.md`](../../README.md)).
5. For unattended processing, use a minimal profile that enables only the MCP servers the worker needs. Do not misdiagnose a client-side MCP startup/lifecycle error as an Anet transport outage.

At the tool boundary, invalid scope expansion raises a permission error, missing initialization raises a runtime error, and invalid claim-settlement actions raise a value error. Client code should surface these as local configuration or request failures, preserve claim tokens only for their rightful owner, and avoid acknowledging work until its result is durably handled. Network delivery status should be determined with the appropriate synchronization, probe, and receipt paths—not inferred solely from successful MCP process startup.
