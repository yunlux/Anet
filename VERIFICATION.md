# Anet public verification record

This file contains publication-safe verification summaries only. Raw deployment
evidence is intentionally excluded because it may contain private paths,
complete Node IDs, addresses, Peer Cards, packet IDs, service names, profile
names, or timing metadata.

The authoritative test procedure is
[`openwiki/testing/verification.md`](openwiki/testing/verification.md).
Each claim below is limited to the strongest gate actually completed.

## Release candidate

- Package: `anet-fabric`
- Version: `0.12.1`
- Python: `>=3.11`
- Platforms targeted by clean runtime installers: Windows, Linux/WSL, macOS
- Default installation behavior: runtime only; no identity, node home, trust
  relationship, profile, or service is created
- Optional Ahub implementation: isolated under explicit `anet.ahub*` modules
  and the `ahub` dependency extra

## Automated verification

The release candidate is required to pass:

```text
python -m ruff check src tests scripts
python -m pytest -q
python -m build
python -m twine check dist/*
```

The repository also verifies:

- cryptographic packet and Peer Card tamper rejection;
- explicit pairing, trust pinning, and local revocation;
- durable queue, consumer lease, task ledger, and cancellation behavior;
- direct, Directory, WebDAV, stdio, and Ahub transport boundaries;
- capability-scoped MCP behavior;
- agent-neutral source and Ahub import-surface isolation;
- clean Windows, Linux/WSL, and macOS runtime installers;
- self-contained Linux Skill installation with a pinned wheel hash.

Observed counts, artifact hashes, and build timestamps must be recorded in the
GitHub Actions run and release checksums generated from the exact release tag.
They are not hard-coded here because rebuilding changes archive hashes.

### 2026-07-25 local release-candidate audit

- Ruff passed for `src`, `tests`, and `scripts`.
- The full suite passed: `272 passed`.
- Dependency audit reported no known vulnerabilities in the installed
  all-extras environment.
- Bandit reported no high-severity findings. The remaining medium findings were
  reviewed as fixed SQL fragments with bound values or URL calls constrained by
  validated HTTP(S) origins.
- WebDAV response parsing was hardened with `defusedxml`.
- Wheel and sdist metadata checks passed; both archives contain Apache License
  2.0, and the sdist contains the Skill and release checklist.
- Text-member scanning found no private keys, user-specific paths, complete
  deployment Node IDs, GitHub tokens, caches, runtime state, or removed
  named-runtime compatibility files in either archive.
- A clean Python 3.11 environment installed the rebuilt `mcp` wheel, imported
  Anet/MCP/`defusedxml`, returned `Anet 0.12.1`, and created zero identity
  files.
- A separate core-only install loaded no `anet.ahub*` modules and installed
  none of MCP, Uvicorn, or WebSockets, confirming the optional dependency and
  import-surface boundary.

The current WSL service layer was unavailable during the rebuilt-wheel
recheck, before Linux or Anet code started. The Skill installer logic had
already passed a fresh-HOME Linux run before the wheel rebuild; the final
rebuilt wheel still requires the GitHub Ubuntu CI and a fresh Linux Skill run
before release promotion.

## Sanitization gate

Before publication, the tracked tree and every reachable Git blob must be
scanned for:

- credentials, private-key PEM blocks, bearer tokens, webhooks, and JWTs;
- complete deployment Node IDs and Peer Card key material;
- user-specific Windows, Linux, WSL, and macOS paths;
- private IPs and deployment ports outside isolated test fixtures;
- named local Agent roles, profiles, services, and carrier destinations;
- node homes, SQLite state, backups, reports, and migration artifacts.

The public branch must contain only placeholders such as `<SOURCE_ROOT>`,
`<ANET_HOME>`, `<NODE_ID>`, `<LAN_ADDRESS>`, and `<SERVICE>`.

## Installation verification

Platform installers use a versioned runtime root and do not initialize a node.
The Linux Hermes Skill installs the pinned `mcp` runtime under
`~/.local/anet`, verifies the CLI and MCP imports, and requires
`identity_files=0`. Creating or binding a persistent node remains a separate,
explicitly authorized operation.

## Claims not made

Automated tests and same-host simulations do not prove:

- physical-device LAN interoperability;
- cross-operator or Internet reachability;
- survival under a specific censorship or DPI system;
- anonymity or global traffic-analysis resistance;
- hardware-backed key protection;
- correctness of a deployment not represented by public evidence.

Publish those claims only after completing the corresponding OpenWiki gate with
sanitized, independently reviewable evidence.
