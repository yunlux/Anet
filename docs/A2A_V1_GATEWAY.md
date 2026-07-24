# A2A 1.0 gateway boundary

Anet's transport, identity, trust, and durable execution protocols do not use
A2A as their wire format. A2A is an optional edge interoperability protocol
above the `anet.agent.task` narrow waist.

The pure mapping lives in `src/anet/a2a_v1.py`; the durable principal,
task/context, and message mapping lives in `PacketStore`. Neither component
starts an HTTP listener, creates a persistent node, automatically trusts a
peer, or performs a network side effect.

## Protocol target

The mapping targets the released A2A 1.0 ProtoJSON data model:

- Agent discovery uses `/.well-known/agent-card.json`;
- endpoint and protocol version are declared in `supportedInterfaces[]`;
- states use `TASK_STATE_*` enum values;
- Message and Artifact content use member-discriminated `Part` values;
- stream values use `statusUpdate` and `artifactUpdate` wrapper members.

It does not accept OmniRoute's v0.3-shaped `skill + messages[]` request or its
post-execution simulated `chunk` stream. A legacy compatibility adapter may be
added separately if an actual deployment requires it.

## Identity and authorization

An A2A Agent Card is not an Anet Peer Card.

- The Agent Card describes a public application endpoint and its public skills.
- The signed Peer Card binds an Anet Node ID to cryptographic keys, locators,
  transport capabilities, and explicit peer trust.
- `build_agent_card` accepts only explicit public skill metadata. It cannot take
  a Peer Card, so Node IDs, locators, transport capabilities, and key material
  are not projected by accident.
- A non-loopback endpoint must use HTTPS. Authentication is required by default;
  an unauthenticated card needs an explicit opt-in intended for controlled local
  development.

The HTTP authentication layer must bind its principal to an authenticated Anet
sender Node ID through `PacketStore.bind_a2a_principal` before calling
`inbound_message_to_task`. Binding requires an explicit local allowlist and is
immutable for that principal. `register_a2a_message` checks the current
authenticated sender against the stored binding on every registration.
Authentication proves provenance, not permission. The existing task ledger
still enforces its independent sender and capability policies before execution.

An inbound request cannot declare its own Anet capabilities. The gateway selects
a locally configured `A2ASkillBinding`, and only that binding supplies
`required_capabilities`.

## Idempotency

For every A2A Message, the mapper derives the 128-bit internal Anet task ID from:

```text
authenticated sender ID × A2A messageId
```

This makes a byte-equivalent A2A retry reach the same internal task. The
gateway mapping and the execution ledger both hash the complete normalized task
body, so reusing a `messageId` with altered content fails closed.

When the client omits `contextId`, the mapper derives a separate opaque context
identifier from the same authenticated scope. A client-provided context ID is
preserved.

The first message creates an external A2A task whose ID equals its first
internal Anet task ID. A follow-up retains that external task and context but
gets a new internal Anet task ID derived from its own `messageId`:

```text
A2A task
  ├── message 1 → Anet task 1
  ├── message 2 → Anet task 2
  └── message N → Anet task N
```

`a2a_gateway_principals`, `a2a_gateway_tasks`, and `a2a_gateway_messages`
persist this relationship. Registration is a `BEGIN IMMEDIATE` transaction, so
concurrent retries create one message row. A follow-up cannot change principal,
sender Node ID, context, tenant, destination peer, skill, or protocol version.

## Recoverable dispatch outbox

Registration also creates one `a2a_gateway_dispatches` intent in the same
transaction as the external Message mapping. The gateway therefore never has a
committed A2A Message whose normalized `agent.task.request` exists only in
process memory.

A bounded local worker uses `claim_a2a_dispatches` to obtain a lease. The claim
contains the already validated body, fixed destination, a fencing token, and a
stable encryption reservation ID. `AnetNode.dispatch_a2a_claim` then:

1. resolves the already trusted destination Peer Card;
2. recovers or reserves the same peer-scoped one-time prekey;
3. seals one immutable `agent.task.request` Packet;
4. atomically marks the prekey used, inserts the Packet, and marks the intent
   dispatched.

If the process dies after prekey reservation but before commit, the next lease
recovers that reservation rather than consuming another key. If sealing or
commit fails, `retry_a2a_dispatch` burns any reserved key, rotates the
reservation ID, and schedules the durable intent for retry. An expired or
superseded claim token cannot commit a Packet or settle another worker's
attempt.

The Packet ID is intentionally not the task idempotency key. A retry after an
unknown remote delivery outcome may produce another Packet, while the receiving
task ledger still collapses both requests by authenticated sender × task ID and
rejects altered bodies.

## Cancellation

The gateway accepts the released A2A 1.0 `CancelTaskRequest` fields `tenant`,
`id`, and `metadata`. Tenant, authenticated principal, and task ID must match
the persisted task. Metadata is treated as untrusted context and cannot choose
an Anet task ID or cancellation reason.

One external A2A task may contain several internal Anet tasks. A cancellation
transaction creates a durable `agent.task.cancel` dispatch for every mapped
internal task and prevents new follow-up Messages. Request and cancel intents
share the recoverable dispatch worker, so a request that is still pending is
ordered before its cancellation locally; receiver-side cancellation tombstones
remain necessary because different Carriers may reorder delivery.

The aggregate `cancel_state` progresses independently from Task state:

```text
requested -> dispatched -> confirmed
                         \-> too_late
```

`dispatched` means every cancellation Packet is durably queued, not that remote
execution stopped. The A2A Task remains non-terminal until a validated canceled
result is appended. A competing completed/failed/rejected result changes
`cancel_state` to `too_late`. Duplicate CancelTask calls return the existing
state and never create additional cancellation Packets.

At the executor, a cancel arriving before its request becomes a durable
tombstone. Active work enters `canceling`; only canceled settlement remains
valid. Cooperative acknowledgement or request-lease takeover fences the old
execution token before the gateway may expose `TASK_STATE_CANCELED`.

## Supported slice

The first slice supports:

- A2A 1.0 Agent Card construction with explicit security;
- new `ROLE_USER` SendMessage requests;
- `text`, `raw`, `url`, and `data` Parts;
- deterministic conversion to `agent.task.request`;
- persistent principal → sender Node ID binding;
- persistent A2A task/context/message → Anet task mapping;
- registration-atomic dispatch intents with leased local workers;
- atomic Packet + one-time-prekey + dispatch settlement;
- idempotent A2A cancellation fan-out across all internal tasks;
- durable receiver tombstones, cooperative stop, and lease-takeover fencing;
- validated multi-turn follow-ups on an existing A2A task;
- append-only aggregate task events with monotonic resumable cursors;
- initial submitted Task construction;
- Anet status/result conversion to A2A status and artifact stream values.

Messages containing an A2A `taskId` require the matching persisted task record.
The mapper infers an omitted context ID from that record and rejects a supplied
mismatch. A task ID without a record is never treated as a new task.

## Next gateway milestone

Before exposing a network endpoint:

1. run interoperability tests with an official A2A 1.0 SDK;
2. only then advertise streaming in a deployed Agent Card.

This remains the mandatory A2A gateway gate, but it is scheduled as roadmap
P2.5. The project-wide P0 is currently the identity/reachability split and a
deployable Rendezvous/Relay/Mailbox ahub. Existing A2A mapping, outbox, event,
and cancellation code remains covered by regression tests while gateway scope
is paused.

Client disconnect must not implicitly cancel a durable task. A2A cancellation
must be stored as intent, delivered to the executor, and reflected as canceled
only after the execution boundary has stopped or fenced further settlement.
