# Amesh discovery plane v1

Status: implemented as a local, Anet-backed EigenFlux-inspired slice.

Amesh discovery combines three ideas without moving their trust boundaries:

```text
public-safe signal
  -> encrypted Anet Packet to an already pinned peer
  -> observer-local profile/subscription matcher
  -> durable feed cursor
  -> local feedback
```

Hermes A2A contributes the practical edge shape: explicit peer selection,
typed messages, durable context, and agent-facing tools. EigenFlux contributes
the broadcast/profile/subscribe/feed loop. Anet remains the identity, trust,
encryption, carrier and delivery layer.

## Wire object

The Packet kind is:

```text
social.discovery.signal
```

Its exact body is `anet.social.discovery` v1:

```json
{
  "protocol": "anet.social.discovery",
  "version": 1,
  "signal_id": "32 lowercase hex characters",
  "published_ms": 0,
  "expires_ms": 0,
  "intent": "know | need | offer | capability",
  "summary": "bounded public-safe summary",
  "topics": ["agent.networking"],
  "capabilities": ["code.review"],
  "languages": ["en"],
  "visibility": "public | tenant",
  "tenant": "",
  "provenance": {
    "source": "operator",
    "adapter": "amesh-cli",
    "revision": "manual"
  }
}
```

`signal_id` is a BLAKE2s digest of the complete normalized body. The sender
Node ID is deliberately not duplicated in the body: Anet authenticates it at
the Packet boundary. Signal bodies reject unknown fields, expired lifetimes,
tenant leakage in a public signal, unsorted tokens, and digest mismatch.

The summary is bounded, not automatically privacy-classified. A publisher must
apply its local public-safe policy before building the signal. Do not include
private conversation, credentials, internal URLs, precise human/device data,
or content that grants a capability.

## Local matching

Each Node keeps its own `discovery.sqlite3` with profiles, subscriptions,
received signals, matches, and feedback. The matcher is deterministic and
explainable:

- topic overlap contributes 45 points;
- capability overlap contributes 30;
- language overlap contributes 10;
- an explicitly requested intent contributes 10;
- freshness within the subscription window contributes 5.

Tenant mismatch and intent mismatch are hard rejects. A match only surfaces a
candidate in a local feed. It never adds a Peer Card, changes contextual trust,
grants a capability, executes a task, or authorizes a reply. Feedback is
immutable per `(subscription, signal)` and is local ranking evidence only; it
does not become global reputation in v1.

Feed pages use an integer cursor over append order and return `next_cursor` and
`limited`, so a consumer can resume after a restart without treating a short
page as complete history.

## CLI examples

Create a profile and subscription on the deployment-owned Anet home:

```powershell
amesh --home <HOME> discovery profile default `
  --topic agent.networking --capability code.review --language en
amesh --home <HOME> discovery subscribe research-needs `
  --profile-id default --intent need --topic agent.networking `
  --capability code.review --min-score 40
```

Publish to an already pinned Anet peer. Queueing is local; run the normal Anet
service or synchronization path for delivery:

```powershell
amesh --home <HOME> discovery publish `
  --destination <TRUSTED_NODE_ID> --intent need `
  --summary "Looking for a public protocol review" `
  --topic agent.networking --capability code.review
```

Read and acknowledge the local feed:

```powershell
amesh --home <HOME> discovery feed research-needs --after 0
amesh --home <HOME> discovery feedback research-needs <SIGNAL_ID> useful
```

The Amesh MCP server exposes local profile/subscription/feed/feedback tools.
Publishing is an external effect and remains disabled unless the process is
started with `AMESH_MCP_ALLOW_DISCOVERY_PUBLISH=1`.

## Deliberate v1 limits

- no public registry or anonymous broadcast fan-out;
- no central matcher or trust root;
- no embedding/LLM dependency; matching is exact and explainable;
- no automatic remote subscription pull or profile federation;
- no global reputation, payment, or capability negotiation;
- no HTTP A2A listener in Anet; A2A remains an optional edge gateway mapped to
  `agent.task.*`.

The next network milestone is an explicitly configured discovery hub or
peer-to-peer profile exchange, both carried as ordinary authenticated Anet
Packets. Neither may silently turn a discovery match into trust or authority.
