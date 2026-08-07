# Agent handoff: standalone Amesh pivot checkpoint

Date: 2026-08-07
Status: checkpoint; implementation is in progress

## User decision

The previous direction was corrected: Amesh must no longer be based on Anet.
Amesh is an independent social-security middleware between Discord/other
platforms and agents. It must own its own identity, permissions, security,
ledgers, relationships, CLI, MCP server, and deployment home.

## Completed in this checkpoint

- Removed the Anet runtime dependency from `amesh/pyproject.toml`.
- Removed all `from anet` / `import anet` usage from `amesh/src` and
  `amesh/tests`.
- Added standalone Amesh identity/pseudonym primitives in `amesh.identity`.
- Replaced Anet relationship types with an Amesh-owned SQLite relationship
  store in `amesh.relations`.
- Moved social scoring and policy evaluation into `amesh.policy`.
- Moved discovery signals and local matching into `amesh.discovery`, using
  generic `source_id` and Amesh-owned `amesh-discovery.sqlite3`.
- Added local agent registry, one-time token issuance, scopes, grants, and
  revocation in `amesh.agent`.
- Replaced the Discord adapter backend with standalone REST, config, ledger,
  pseudonym, rate-limit, reply reservation, and allowlist code.
- Updated CLI/MCP to use `AMESH_HOME`, local discovery outbox, and agent grants.
- Removed the Amesh discovery hooks and private database protections from the
  Anet node and Anet release gates.
- Rewrote Amesh README/deployment/discovery/architecture docs and added the
  standalone rule to `AGENTS.md`.

## Verification so far

- `python -m compileall -q amesh/src src/anet`: passed
- Amesh tests: `65 passed`
- CLI smoke test: agent register/list passed; token is returned only during
  registration and omitted from list output.

## Remaining work for the next agent

1. Run the full Anet regression suite after removing the old Anet discovery
   module and node hooks.
2. Run `git diff --check` and inspect all staged files for stale Anet wording or
   accidental unrelated changes.
3. Add/finish standalone Discord backend tests and agent-token effect tests if
   needed.
4. Update any remaining Amesh deployment/source-map references, then create the
   final commit and report both checkpoint and final commit hashes.

## Important boundaries

- Discovery is candidate ranking only; it cannot grant platform permissions or
  agent scopes.
- Non-operator effects require a matching agent token, scope, and explicit
  adapter/action grant.
- Discord tokens stay in an environment variable selected by config and are
  not persisted.
- No implicit Anet, A2A, or other transport is used by the Amesh core.
