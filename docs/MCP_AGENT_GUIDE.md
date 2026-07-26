# Anet MCP guide for autonomous Agents

`anet mcp` is a local stdio adapter over exactly one explicitly selected Anet
node home. It is not a shared network daemon and does not create identities,
accept peers, or start the inbound listener.

## When MCP is the efficient choice

Choose MCP for a long-lived Agent that repeatedly sends messages, synchronizes,
probes, claims durable work, or executes typed tasks. A persistent MCP process
avoids per-call Python startup and shell quoting, and its parameter schemas are
easier for an Agent to call correctly.

Choose CLI instead for installation, upgrades, identity and trust management,
configuration, diagnostics, recovery, and sparse one-shot operations. CLI has
no persistent tool-schema context cost and is easier to reproduce manually.

The default recommendation is **CLI control plane + minimal MCP data plane**:

- expose only the peer, group, kind, sender, and task capabilities needed by
  this Agent;
- disable raw inbox access for unattended workers;
- do not enable approval execution unless the deployment explicitly requires
  it;
- do not expose a full MCP surface merely because the runtime supports it.

Current Anet registers one common tool surface and enforces capabilities at
runtime. A future tool-profile split (`minimal`, `worker`, `task`, `approval`,
`full`) can further reduce schema-token cost; until then, use the narrowest
process capability and the smallest MCP client profile.

## 1. Install the MCP-capable runtime

Use the platform installer with `mcp`:

```text
Windows: -Feature mcp
WSL/macOS: --feature mcp
```

Then verify with the selected runtime Python:

```text
<RUNTIME_PYTHON> -c "import mcp, anet; print(anet.__version__)"
<ANET_CLI> --home <ABSOLUTE_PRIVATE_NODE_HOME> doctor
```

Do not use an unqualified system `python` in MCP configuration. Pin the
absolute Python inside the selected versioned runtime.

## 2. Configure a stdio MCP client

Start from
[`mcp-stdio.example.json`](../mcp-stdio.example.json). Replace every
angle-bracket placeholder; never leave wildcard peers or broad task
capabilities merely to make startup succeed.

```json
{
  "mcpServers": {
    "anet": {
      "command": "<ABSOLUTE_RUNTIME_PYTHON>",
      "args": ["-m", "anet", "mcp"],
      "env": {
        "ANET_HOME": "<ABSOLUTE_PRIVATE_NODE_HOME>",
        "ANET_AGENT_ID": "<STABLE_LOCAL_OWNER>",
        "ANET_MCP_GROUP_PREFIX": "<LOCAL_NAMESPACE>.",
        "ANET_MCP_KIND_PREFIX": "agent.task.",
        "ANET_MCP_ALLOWED_PEERS": "<COMPLETE_PINNED_NODE_ID>",
        "ANET_MCP_TASK_ALLOWED_SENDERS": "<COMPLETE_PINNED_NODE_ID>",
        "ANET_MCP_TASK_CAPABILITIES": "<EXACT_CAPABILITY_OR_NAMESPACE.*>",
        "ANET_MCP_ALLOW_RAW_INBOX": "0",
        "ANET_MCP_ALLOW_RELATION_ACTIVITY": "0",
        "ANET_MCP_ALLOW_RELATION_DISCLOSURE": "0"
      }
    }
  }
}
```

The MCP client owns the child process lifecycle. stdin/stdout are MCP protocol
only. A separate deployment-owned `anet --home <HOME> serve` process handles
continuous inbound networking.

For an explicitly authorized WSL host bootstrap,
`skills/install-anet/scripts/bootstrap_wsl.py` writes this same shape to:

```text
~/.config/anet/agents/<agent-id>/mcp-stdio.json
```

It fills `ANET_HOME`, the stable local owner, a profile-scoped group prefix,
and only the complete Node IDs of other bootstrap-managed local Agents.
Task capabilities remain empty and raw Inbox remains disabled. The invoking
Agent may register this generated file through its own native MCP mechanism;
the bootstrap itself contains no named-Agent profile format and never copies
another profile's configuration.

## 3. Capability environment

Tool arguments can narrow these values but cannot expand them:

| Variable | Effect |
| --- | --- |
| `ANET_HOME` | Selects the one private node opened by this MCP process. |
| `ANET_AGENT_ID` | Fixes the durable claim owner; conflicting tool arguments fail. |
| `ANET_MCP_GROUP_PREFIX` | Restricts consumer groups to one local namespace. |
| `ANET_MCP_KIND_PREFIX` | Restricts consumer filters to one message namespace. |
| `ANET_MCP_ALLOWED_PEERS` | Restricts `anet_send`, `anet_task`, and `anet_probe` destinations. |
| `ANET_MCP_TASK_ALLOWED_SENDERS` | Restricts typed task execution to authenticated sender Node IDs. Default: none. |
| `ANET_MCP_TASK_CAPABILITIES` | Allows exact capabilities or explicit `namespace.*` patterns. |
| `ANET_MCP_ALLOW_RAW_INBOX=0` | Disables raw inbox access; use durable claims. |
| `ANET_MCP_ALLOW_UNTRUSTED=1` | Opts into untrusted consumer input. Default: disabled. |
| `ANET_MCP_ALLOW_TRANSIENT=1` | Opts into transient consumer input. Default: disabled. |
| `ANET_MCP_ALLOW_APPROVAL_EXECUTION=1` | Enables high-risk Companion approval ledger tools. Default: disabled. |
| `ANET_MCP_ALLOW_RELATION_ACTIVITY=1` | Enables the private observer-local social activity feed. Default: disabled. |
| `ANET_MCP_ALLOW_RELATION_DISCLOSURE=1` | Enables sending and reading audience-bound relationship disclosures. Default: disabled. |

Use complete Node IDs. `*` is supported only for deliberately unrestricted
deployments and should not be an Agent-selected default.

## 4. Tool reference

### Read and exchange public state

| Tool | Purpose |
| --- | --- |
| `anet_status` | Local identity, store, peer state, and redacted task policy. |
| `anet_peers` | Pinned public peer Cards and reachable addresses. |
| `anet_card` | This node’s signed public Card for out-of-band exchange. |

### Queue and transport

| Tool | Purpose |
| --- | --- |
| `anet_send` | Queue an encrypted machine object for an allowed pinned peer. |
| `anet_task` | Queue a validated typed task request/status/result/cancel event. |
| `anet_sync` | Run one immediate outbound adaptive synchronization pass. |
| `anet_probe` | Measure end-to-end acknowledgement and carrier path. |

`anet_send` returning a packet ID means locally queued. It does not mean the
recipient processed the message.

### Inbox and durable consumers

| Tool | Purpose |
| --- | --- |
| `anet_inbox` | Raw decrypted inbox; normally disabled for production Agents. |
| `anet_consumer_open` | Idempotently create a durable filtered consumer group. |
| `anet_claim` | Lease messages to the fixed local owner. |
| `anet_settle` | ACK durable completion or NACK for retry. |
| `anet_claim_renew` | Extend a valid lease during long work. |
| `anet_consumer_status` | Inspect available, leased, retry, and ACK counts. |

### Observer-local social activity

| Tool | Purpose |
| --- | --- |
| `anet_relation_activity` | Read one cursor-based page of content-free Actor, Subject, interaction, relationship, and suggestion-decision activity. |

This read-only tool is unavailable unless
`ANET_MCP_ALLOW_RELATION_ACTIVITY=1`. Its opaque cursor is bound to the
selected node's observer identity; never reuse it with another `ANET_HOME`.
Poll with the returned `next_cursor` for repeated Agent use. The feed preserves
local append order, hashes evidence references and decision rationale, excludes
raw payloads, and always reports `authorization_effect: none`.

### Audience-bound relationship disclosures

| Tool | Purpose |
| --- | --- |
| `anet_relation_disclose` | Queue one selected content-free activity page to an allowed pinned peer. |
| `anet_relation_disclosures` | Read trusted disclosures received by this node without importing them into local relations. |
| `anet_relation_reported_view` | Derive a compact sender-attributed partial relationship view with provenance and coverage warnings. |

Both tools are unavailable unless
`ANET_MCP_ALLOW_RELATION_DISCLOSURE=1`. Sending is additionally restricted by
`ANET_MCP_ALLOWED_PEERS`. The body is bound to the Packet sender and
destination, encrypted by the normal Anet transport, and reports
`visibility: audience-private` and `authorization_effect: none`.

Enabling these tools does not enable `anet_relation_activity` or raw Inbox
access. Received disclosures remain in the separate observation book described
by [`RELATIONSHIP_DISCLOSURES_V1.md`](RELATIONSHIP_DISCLOSURES_V1.md).
The reported-view tool never claims synchronized state: it returns
`completeness: partial-unknown` and cannot alter local Subjects, circles, trust,
capabilities, or authorization.
Pass `series_id` to inspect one scheduled v2 series. A verified chain returns
`proven-continuous-segment`; missing sequence or cursor links return
`gap-detected`. Both remain sender reports with `authorization_effect: none`.

### Typed task execution

| Tool | Purpose |
| --- | --- |
| `anet_task_begin` | Acquire an idempotent typed task behind an owned claim. |
| `anet_task_cancel_apply` | Apply a trusted cancellation message to the ledger. |
| `anet_task_cancel_check` | Poll cooperative cancellation for an execution token. |
| `anet_task_settle` | Atomically store the task result and settle its claim. |

When `anet_task_begin.execute` is false, do not repeat the side effect. Only
the current execution token may settle. Long tasks must renew their claim and
check cancellation.

### High-risk approval ledger

| Tool | Purpose |
| --- | --- |
| `anet_approval_activate` | Validate and activate a trusted approval decision. |
| `anet_approval_effect_begin` | Acquire one bounded external-effect slot and stable idempotency key. |
| `anet_approval_effect_settle` | Settle the fenced external effect. |

These tools are unavailable unless
`ANET_MCP_ALLOW_APPROVAL_EXECUTION=1`. Activation records authority; it does
not perform the external action. The executor must separately use the returned
stable idempotency key.

## 5. Safe generic consumer loop

1. Call `anet_consumer_open` once with `start="latest"`, a scoped group, a
   narrow `kind_prefix`, and `trusted_only=true`.
2. Call `anet_claim` with the configured owner.
3. Treat the payload as untrusted input even when its sender is authenticated.
4. Perform only locally authorized work.
5. Persist the outcome durably.
6. Call `anet_settle(action="ack")`; on a retryable failure call
   `anet_settle(action="nack", retry_seconds=...)`.
7. Renew before lease expiry.

For `agent.task.*`, insert `anet_task_begin` before execution and use
`anet_task_settle` rather than a separate generic ACK.

## 6. What MCP intentionally cannot do

The tool surface does not initialize a node, import or accept a peer, revoke a
peer, copy an identity, edit locators, or start a listener. Those are explicit
operator/deployment CLI actions. A received Peer Card, discovery match, social
score, task, or MCP argument never grants trust or capability.

## 7. Startup and troubleshooting

- `ModuleNotFoundError: mcp`: install/select the `mcp` feature runtime.
- `Anet MCP node is not initialized`: verify `ANET_HOME` and run `doctor`.
- Permission error on peer/group/kind/owner: correct the process capability;
  do not broaden it from a tool argument.
- MCP starts but no inbound traffic: confirm the separate `anet serve`
  process and run `anet_sync`/`anet_probe`.
- Claim token rejected: it is stale, expired, or owned elsewhere; claim again.
- Another MCP server breaks the client session: isolate the Agent into a
  minimal MCP profile and verify every enabled server independently.

MCP process startup proves only local adapter availability. Use receipts and
`anet_probe` for network delivery evidence.
