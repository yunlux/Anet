# Agent handoff: durable Amesh route/outbox state machine

Date: 2026-08-07
Status: completed

## Objective

Deliver handoff item 2 from the standalone pivot: a durable route/outbox state
machine with retry, deduplication, and destination-specific policy checks.

## Completed

- Added `amesh.route.RouteStore` (SQLite, `amesh-routes.sqlite3`):
  - states `pending / retrying / delivered / failed / expired`;
  - idempotent enqueue, deduplicated per (destination, signal_id);
  - exponential-backoff delivery with `MAX_ATTEMPTS` and signal-expiry
    handling;
  - destination/adapter policy rules (`amesh_route_policy`) with a
    configurable `default_allow` (fail-closed option).
- `amesh serve` now enqueues adapter signals into the outbox, runs a delivery
  worker, and drains remaining due routes on shutdown; the result reports route
  counts alongside the delivered signal count.
- CLI: `amesh route status|list|retry|flush|policy|policy-list`.

## Verification

- Amesh tests: `78 passed` (77 prior + 1 CLI route test).
- ruff check / format: clean.
- No `anet` / `ANET_HOME` references introduced; Amesh remains standalone.

## Boundaries

- Delivery currently writes to the outbound directory sink; a future transport
  or agent connector replaces the `deliver` callback.
- Policy defaults to allow for backward compatibility; use
  `RouteStore(default_allow=False)` for a fail-closed home.

## Next step

Add Discord gateway/event-stream support only after REST polling and effect
permissions have stable integration coverage (handoff item 3), or keep A2A /
Anet / future platforms as optional adapters outside this core.
