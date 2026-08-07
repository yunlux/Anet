# Agent handoff: Anet social discovery v1

Date: 2026-08-07
Status: implemented and verified

## Objective

Continue the Anet research with emphasis on Hermes A2A and EigenFlux, and turn
the useful parts into an Anet-based social system without weakening Anet's
identity, trust, encryption, or durable-delivery boundaries.

## What was implemented

- Added the `social.discovery.signal` / `anet.social.discovery` v1 contract in
  `src/anet/discovery.py`.
- Added digest-bound signal IDs, exact-field validation, bounded TTL, public vs
  tenant visibility, provenance, and public-safe payload limits.
- Added observer-local `discovery.sqlite3` storage for profiles, subscriptions,
  signals, explainable matches, cursor-based feeds, and immutable feedback.
- Integrated trusted discovery packet validation and local ingestion into
  `src/anet/node.py`.
- Added Amesh CLI and MCP management for profiles, subscriptions, feed,
  feedback, ingest, and publish.
- Added end-to-end Anet and Amesh tests, documentation, and private node-home
  release-gate protection for `discovery.sqlite3`.

## Boundary decisions

- Anet remains the identity, trust, encryption, carrier, and delivery layer.
- Amesh owns local social projection and matching; a match never grants trust,
  capability, authorization, or a new peer relationship.
- The current slice has no public registry, anonymous fan-out, central matcher,
  LLM/embedding dependency, global reputation, or HTTP A2A listener.
- Publish-to-peer requires an already trusted destination. External MCP publish
  is gated by `AMESH_MCP_ALLOW_DISCOVERY_PUBLISH=1`.

## Verification

- Amesh suite: `63 passed`
- Anet suite: `535 passed`
- Ruff targeted/full relevant checks: passed
- `git diff --check`: passed

## Key files

- `src/anet/discovery.py`
- `src/anet/node.py`
- `amesh/src/amesh/cli.py`
- `amesh/src/amesh/mcp_server.py`
- `docs/AMESH_DISCOVERY_V1.md`
- `tests/test_discovery.py`
- `amesh/tests/test_amesh_discovery.py`

## Recommended next step

Implement an explicitly configured discovery hub or profile-exchange flow over
Anet Packets, with privacy projection tests and cursor/gap recovery. After that,
add an optional A2A edge adapter that maps Agent Card skills to discovery
capabilities; keep it outside the Anet trust and authorization core.
