# Contributing to Anet

Anet is an Alpha encrypted messaging fabric. Changes at identity, trust,
packet, storage, and transport boundaries require tests and explicit
compatibility analysis.

## Development setup

Use Python 3.11 or newer:

```bash
python -m venv .venv
python -m pip install -e ".[test,mcp,ahub]"
python -m ruff check src tests scripts
python -m pytest -q
```

Do not use a real persistent node home in tests. Use disposable temporary homes
and never promote them into a deployment.

## Pull requests

- Keep source and deployment assets Agent-neutral. Operator-owned homes, labels,
  peers, destinations, ports, profiles, and service names must be inputs.
- Keep Ahub optional and outside the default `anet` and `anet.carriers` import
  surfaces.
- Add or update tests for behavior and failure handling.
- Update source documentation rather than hand-editing generated OpenWiki pages.
- Run the complete test suite, Ruff, package build, and archive inspection.
- Avoid drive-by formatting or unrelated refactors.

## Sensitive data

Never commit credentials, private keys, complete Node IDs, Peer Cards, node
homes, SQLite state, local reports, backups, migration archives, private
addresses, user-specific paths, or deployment/profile names. Examples must use
placeholders. If a secret is committed, revoke or rotate it before attempting
history cleanup.

By submitting a contribution, you agree that it is licensed under the repository
Apache License 2.0.
