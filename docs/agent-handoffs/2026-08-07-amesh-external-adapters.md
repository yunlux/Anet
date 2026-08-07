# Agent handoff: pluggable external adapters

Date: 2026-08-07
Status: completed

## Objective

Let A2A, Anet, and future platforms live as optional adapters outside the
Amesh core without any runtime import of another application's models.

## Completed

- Added `amesh.adapter.register_adapter(name, factory)` (in-process registry)
  and the `amesh.adapters` setuptools entry-point group.
- `adapter_names()` merges built-in, registered, and entry-point adapters;
  `load_adapter()` resolves built-in -> registered -> entry-point, and rejects
  a factory that does not produce a `PlatformAdapter`.
- CLI/MCP/connector/serve now use `adapter_names()` so discovered adapters are
  reachable; `amesh adapter list` tolerates adapters that fail to load and
  reports the load error inline.

## Verification

- Amesh tests: `79 passed` (78 prior + 1 registry test covering registration,
  discovery, duplicate/name collision rejection, and non-callable rejection).
- ruff check / format: clean.
- No `anet` / `ANET_HOME` references introduced; Amesh core stays standalone.

## Boundaries

- An external adapter still implements the `PlatformAdapter` contract and owns
  its own platform integration; discovery never grants it scopes, permissions,
  or trust.

## Next step

Option A: Discord gateway/event-stream support once REST polling and effect
permissions have stable integration coverage. Option B: publish an optional
Anet (or A2A) adapter package that registers itself via `amesh.adapters`.
