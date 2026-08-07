# Agent handoff: Amesh agent connector

Date: 2026-08-07
Status: completed

## Objective

Deliver handoff item 1 from the standalone pivot: an explicit agent connector
contract that performs token authentication at the adapter boundary and records
request audit.

## Completed

- Added `amesh.connector` (stdlib only): `EffectConnector` is a loopback
  `POST /v1/effects` boundary. A request carries `Authorization: Bearer
  <token>` and a JSON body `{adapter, action, event_key, content}`.
- Authentication: the bearer token is validated against the Amesh agent
  registry; the adapter then enforces `require_agent` (token match, enabled
  agent, declared scope, explicit adapter/action `allow` grant).
- Effect execution: only `reply` is a supported effect action; the adapter's
  own evidence/permission thresholds still apply on top of the agent grant.
- Audit: `ConnectorAudit` keeps an append-only `amesh-audit.sqlite3` with the
  authenticated agent, adapter, action, event, outcome (authorized / denied /
  rejected), HTTP code, and error text. Tokens never appear in audit.
- CLI: `amesh connector serve --adapter NAME [--host] [--port]` and
  `amesh connector audit [--limit]`.

## Verification

- Amesh tests: `69 passed` (65 prior + 4 new connector tests).
- ruff check / format: clean.
- CLI smoke: agent register + grant, `connector serve --port 0`, valid-token
  effect returned 200/sent, bad token returned 401, `connector audit` listed
  both authorized and rejected rows.
- Amesh remains standalone: no `anet` / `ANET_HOME` references introduced.

## Boundaries

- Connector binds to loopback by default; it never exposes tokens or platform
  secrets.
- It is a read/reply boundary, not a supervisor: it does not take the serve
  home lock. Do not run it against a home while another process mutates the
  same ledger unless the platform store supports concurrent writers.

## Next step

Add a durable route/outbox state machine with retry, deduplication, and
destination-specific policy checks (handoff item 2).
