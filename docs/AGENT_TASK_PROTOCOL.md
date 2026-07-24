# Anet typed Agent task protocol

Anet transports opaque encrypted objects. It does not decide what an Agent may
do. This adapter defines a small, deterministic task envelope above that narrow
waist so heterogeneous Agent runtimes, A2A gateways, and future
broadcast/matching services can
exchange work without using natural language as the wire protocol.

## Message kinds

All bodies contain:

```json
{
  "protocol": "anet.agent.task",
  "version": 1,
  "task_id": "32-lowercase-hex-characters"
}
```

The supported kinds are:

| Kind | Purpose |
| --- | --- |
| `agent.task.request` | Objective, typed input, required capabilities, and context |
| `agent.task.status` | Non-terminal state, progress, and a short diagnostic message |
| `agent.task.result` | Terminal state plus output or a required failure reason |
| `agent.task.cancel` | Cancellation request with an explicit reason |

`task_id` is stable across status and result messages. A caller may supply it
before the first send. Packet ID identifies one encrypted network object; task
ID identifies the logical unit of work.

States follow the A2A task lifecycle vocabulary where it fits Anet:
`submitted`, `working`, `input-required`, `auth-required`, `completed`,
`failed`, `canceled`, and `rejected`. Terminal states are accepted only in a
result message.

## MCP adapter

The `anet_task` tool constructs and sends validated task events:

```text
anet_task(
  to_node=<pinned Node ID>,
  operation="request",
  objective="review this patch",
  payload={"commit": "abc"},
  required_capabilities=["code.review"]
)
```

Use the returned `task_id` for later `status`, `result`, or `cancel`
operations. `ANET_MCP_ALLOWED_PEERS` applies exactly as it does to `anet_send`.
The adapter validates the envelope before queueing it.

### Durable execution and side-effect idempotency

Receiving a request uses the normal durable consumer flow followed by a task
ledger acquisition:

```text
anet_consumer_open(group="runtime.tasks", kind_prefix="agent.task.request")
claim = anet_claim(group="runtime.tasks")
execution = anet_task_begin(
  group="runtime.tasks",
  claim_token=claim.claim_token
)

if execution.execute:
    # Apply local authorization, then perform and durably commit the work.
    anet_task_settle(
      group="runtime.tasks",
      claim_token=claim.claim_token,
      execution_token=execution.execution_token,
      state="completed",
      payload={"result": "..."}
    )
```

The persistent idempotency key is:

```text
consumer group × authenticated sender Node ID × task ID
```

The ledger also stores a canonical request hash. A retry with the same key and
identical request is deduplicated. If the first execution is complete, the
duplicate claim is ACKed and its stored result is returned with
`execute=false`. A task ID reused with different request content fails closed.
Different consumer groups remain deliberate fan-out boundaries.

Before the ledger can be acquired, the MCP process applies two independent,
fail-closed inbound policies:

```powershell
$env:ANET_MCP_TASK_ALLOWED_SENDERS = "<NODE_ID_1>,<NODE_ID_2>"
$env:ANET_MCP_TASK_CAPABILITIES = "code.review,health.*,artifact.read"
```

`ANET_MCP_TASK_ALLOWED_SENDERS` is separate from the outbound
`ANET_MCP_ALLOWED_PEERS`. An unset or empty sender policy authorizes no inbound
task execution. `ANET_MCP_TASK_CAPABILITIES` accepts exact capabilities and
explicit namespace patterns ending in `.*`; `code.*` matches `code.review` but
not the bare `code` capability. An unset capability policy permits only
requests whose `required_capabilities` list is empty. A literal `*` enables the
corresponding whole policy and must be an explicit operator choice.

Both the authenticated sender and every requested capability must pass before
an execution row is created. Tool arguments cannot add senders or capability
patterns. `anet_status` reports only the sender count, wildcard state, and
capability patterns so an operator can diagnose a fail-closed profile without
exposing the sender Node IDs in routine status output. Native adapters that call
`PacketStore.begin_agent_task` directly must pass equivalent explicit policy
sets.

`anet_task_settle` writes the terminal result and ACKs the consumer claim in one
SQLite transaction. `state=retry` releases both records together after an
optional delay. Only the current execution token can settle; after lease
takeover, the prior worker's token is stale. Once `anet_task_begin` succeeds,
the generic `anet_settle` path is rejected so it cannot bypass the atomic task
result invariant. `anet_claim_renew` remains valid for long-running work.

Cryptographic sender authentication is provenance, not authorization.
The MCP policy maps `required_capabilities` to a local execution boundary, but
the worker must still validate parameters, budgets, and action-specific
approval. It must commit external side effects durably before task settlement. The ledger
prevents Anet from issuing the same logical execution twice within a consumer
group, but it cannot make an external API idempotent after a crash between that
API call and the local transaction. Use `task_id` as the downstream
idempotency key whenever the external system supports one. A task cancellation
is a request, not proof that execution stopped.

### Durable cooperative cancellation

A worker that accepts both requests and cancellations must open its group with
`kind_prefix="agent.task."`. Cancellation is not handled by a generic ACK:

```python
cancel = anet_task_cancel_apply(
    group="runtime.tasks",
    claim_token=cancel_claim.claim_token,
)
```

The cancellation key is consumer group × authenticated sender × task ID. A
different trusted sender cannot cancel another sender's task merely by knowing
its task ID. Repeating the same cancellation body is idempotent; changing its
reason under the same key fails closed.

Cancellation may arrive before the request because Carriers are allowed to
reorder packets. In that case the ledger stores a durable tombstone. When the
request later arrives, `anet_task_begin` ACKs it as canceled without issuing an
execution token or running side effects.

For active work, applying the cancellation changes the ledger state from
`working` to internal state `canceling` but keeps the current execution token so
the worker can stop cleanly:

```python
cancel = anet_task_cancel_check(
    group="runtime.tasks",
    execution_token=execution.execution_token,
)
if cancel:
    stop_local_work()
    anet_task_settle(
        group="runtime.tasks",
        claim_token=request_claim.claim_token,
        execution_token=execution.execution_token,
        state="canceled",
        error=cancel.reason,
    )
```

While `canceling`, completed, failed, rejected, or retry settlement is refused;
only `canceled` is accepted. Generic consumer ACK/NACK is also blocked. If the
worker disappears, request-claim lease takeover finalizes cancellation and
clears the old execution token, so the former worker can no longer settle.

If terminal settlement commits before the cancellation transaction, the
cancellation is recorded as `too_late` and cannot rewrite the result. If the
cancellation transaction commits first, later completion is rejected. This
serializable boundary prevents both outcomes from becoming durable.

Fencing prevents stale Anet settlement; it cannot roll back an external side
effect already committed before the worker observed cancellation. Long-running
executors must check between bounded steps, propagate cancellation to child
processes where possible, and use downstream idempotency/compensation.

## Interoperability boundary

This schema is intentionally smaller than A2A and does not copy EigenFlux's
central AI matching engine:

- an A2A gateway can map Agent Card skills to `required_capabilities` and A2A
  task state to Anet task state;
- a broadcast/index service can advertise task requests without changing the
  encrypted packet, identity, trust, or Carrier protocols;
- runtime-specific details belong in `input`, `output`, or a versioned gateway
  mapping, not in Anet's transport core.

Unknown protocol versions and malformed bodies fail closed through
`validate_task_message`.

The first A2A 1.0 pure mapping is documented in
[`A2A_V1_GATEWAY.md`](A2A_V1_GATEWAY.md). It uses sender-scoped deterministic
task IDs, explicit public skill bindings, exact v1 state/Part shapes, and a
durable principal/task/context/message mapping. Multi-turn messages retain one
external A2A task while each message receives its own idempotent internal Anet
task. No HTTP listener is exposed yet.
