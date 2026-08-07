# Amesh standalone architecture

Status: standalone pivot implemented in the Amesh subproject.

## Purpose

Amesh is the middleware between social platforms and agents. It is not a
transport fabric, an agent runtime, or a platform identity provider. The
project is intentionally installable and runnable without Anet, A2A SDKs, or
another application's database.

## Core versus adapters

```text
Platform API / agent connector
          |
      Adapter boundary
          |
  bounded event + source auth
          |
 private ledger / pseudonyms
          |
  policy + actor permissions
          |
  agent registry + grants
          |
 CLI / MCP / Amesh outbox
```

Core modules:

- `identity.py`: one private Amesh installation key and stable local
  pseudonyms; it is not a platform token or another project's identity.
- `policy.py`: evidence scoring, action thresholds, operator rules, and an
  append-only permission decision audit.
- `agent.py`: local agent registry, one-time bearer token issuance, scopes,
  explicit adapter/action grants, revocation, and token-digest storage.
- `relations.py`: observer-local relationship projection owned by Amesh.
- `signal.py` and `discovery.py`: bounded envelopes and local discovery feeds;
  neither can grant authority.

Adapters:

- `adapters/discord.py`: Discord REST v10 polling, allowlisted guild/channel
  ingestion, pseudonymous ledger, safe bounded content, reply reservation, and
  rate-limit/error categories.
- `adapters/loopback.py`: offline spool adapter used for development and
  integration tests.

## Security invariants

1. Platform account IDs are converted to Amesh-local pseudonyms before they
   enter shared signals or relationship records.
2. Discord tokens are read from an operator-selected environment variable and
   never persisted in JSON configuration or error text.
3. Discord polling is limited to configured guild/channel IDs; content is
   metadata-only unless the event is explicitly addressed to Amesh.
4. Evidence thresholds and actor permission rules are both required for
   platform effects. A permission rule can remove an action, never invent
   evidence for it.
5. Non-operator agent effects require a matching bearer token, an enabled agent
   record, a declared scope, and an explicit adapter/action `allow` grant.
6. Agent, platform, and discovery identities are not interchangeable.
7. Discovery is candidate ranking only. It never mutates agent grants,
   platform permissions, relationship state, or trust roots.

## Current control surfaces

- CLI: adapter status, social ingest/reply, agent register/grant/revoke, local
  discovery profiles/feed/feedback, and one long-lived `serve` process per
  Amesh home.
- Connector: a local loopback HTTP boundary (`amesh connector serve`) where
  agents authenticate with one-time bearer tokens and execute granted effects;
  every request is recorded in the append-only `amesh-audit.sqlite3`.
- MCP: read operations and local mutations by default; reply and discovery
  publishing are separately gated by `AMESH_MCP_ALLOW_REPLY=1` and
  `AMESH_MCP_ALLOW_DISCOVERY_PUBLISH=1`.
- Outbox: a durable route/outbox state machine (`amesh-routes.sqlite3`) queues
  one route per (destination, signal_id) with deduplication, exponential-backoff
  retry, signal expiry, and destination/adapter policy rules. Delivery writes
  validated signal files; there is no implicit Anet or A2A delivery path.

## Next work

1. Add Discord gateway/event-stream support only after REST polling and effect
   permissions have stable integration coverage.
2. Keep A2A, Anet, and future platforms as optional adapters outside this core.
