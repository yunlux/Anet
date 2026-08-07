# Agent handoff: Amesh session summary

Date: 2026-08-07
Status: completed

## Objective

Take over the standalone Amesh pivot and advance the next implementation items
without reintroducing an Anet runtime dependency.

## Session work

1. Verified the Codex standalone pivot: Amesh has no `anet` imports,
   `anet-fabric` dependency, or `ANET_HOME` references; removed two unused
   imports ruff reported.
2. Delivered handoff item 1 — agent connector contract:
   `amesh.connector` (loopback HTTP `POST /v1/effects`), bearer-token
   authentication against the agent registry, explicit adapter/action grant,
   and append-only request audit (`amesh-audit.sqlite3`).
3. Delivered handoff item 2 — durable route/outbox state machine:
   `amesh.route.RouteStore` with per-(destination, signal_id) deduplication,
   exponential-backoff retry, signal expiry, destination/adapter policy rules,
   and a delivery worker inside `amesh serve` (final drain on shutdown).
4. Delivered handoff item 4 — pluggable external adapters:
   `amesh.adapter.register_adapter()` plus the `amesh.adapters` entry-point
   group, so Anet/A2A/future platforms can ship as separate packages without
   the core importing their models.

## Changed files (this session, after the pivot)

- `amesh/src/amesh/connector.py` (new), `route.py` (new)
- `amesh/src/amesh/adapter.py`, `cli.py`, `serve.py`, `mcp_server.py`
- `amesh/adapters/discord_backend.py`, `relations.py` (import cleanup)
- `amesh/tests/test_connector.py` (new), `test_route.py` (new),
  `test_adapter.py`, `test_cli.py`
- `amesh/README.md`, `docs/AMESH_STANDALONE_ARCHITECTURE.md`
- Handoffs: `2026-08-07-amesh-connector.md`, `2026-08-07-amesh-route-outbox.md`,
  `2026-08-07-amesh-external-adapters.md`

## Verification

- Amesh tests: `79 passed`; ruff check/format clean; `git diff --check` clean.
- Anet regression suite: `534 passed`; compileall passed.
- Worktree clean at handoff.

## Open decisions

- Route delivery currently writes to the outbound directory sink; a real
  transport or agent connector replaces the `deliver` callback.
- Route policy defaults to allow for backward compatibility; a fail-closed
  home uses `RouteStore(default_allow=False)`.
- A separate session fixed Anet mypy errors (`7fd878f`, `src/anet/` only);
  no Amesh overlap.

## Next recommended step

Discord gateway/event-stream support once REST polling and effect permissions
have stable integration coverage, or publish an optional Anet/A2A adapter
package that registers itself via `amesh.adapters`.
