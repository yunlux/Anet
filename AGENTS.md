<!-- OPENWIKI:START -->

## OpenWiki

This repository uses OpenWiki for recurring code documentation. Start with `openwiki/quickstart.md`, then follow its links to architecture, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

## Agent self-service

- For a platform install or CLI workflow, read `docs/CLI_AGENT_GUIDE.md`.
- For stdio MCP setup and tool semantics, read `docs/MCP_AGENT_GUIDE.md` and
  start from `mcp-stdio.example.json`.
- To bootstrap a new Linux Hermes Agent from a distributable Skill, use
  `skills/install-anet` and `docs/HERMES_SKILL_INSTALL.md`.
- Installing the runtime does not authorize persistent node creation, trust
  changes, identity copying, or service registration. Keep those as separate,
  explicit actions.
- Use CLI as the control plane for install, upgrade, identity, trust,
  configuration, diagnostics, and recovery. Use MCP as the data plane for a
  long-lived Agent's frequent messaging, durable claims, and typed tasks.
  Prefer CLI for sparse one-shot work; prefer a narrowly scoped MCP session
  for repeated work. Do not expose the full MCP capability set by default.

## Persistent-node safety

- Read `openwiki/operations/onboarding-and-recovery.md` before creating, moving, pairing, or repairing a persistent node.
- Never create a new persistent node merely because a profile, label, or expected path is missing. First locate the deployment-owned `ANET_HOME` and confirm its complete Node ID.
- One runtime owns one private node home. Never copy `identity.json`, `tls-key.pem`, SQLite state, or an entire home between Windows, WSL, macOS, profiles, or workers.
- Labels and IP addresses are not identity. WSL mirrored networking can share an IP with Windows while remaining a different node; use distinct ports and Node IDs.
- Use `locator-config` for address/context changes, then redistribute the newly signed Card. Do not hand-edit `config.json` or `card.json`.
- Ephemeral workers use no persistent Anet node by default. If a test requires one, use a disposable home and never promote it.

## Agent-neutral source

- Anet source, protocol names, deployment assets, defaults, and examples must not
  contain code dedicated to a named Agent or organizational role.
- Platform-specific implementations are allowed when the platform boundary is
  explicit and all node homes, labels, service instances, peers, and destinations
  are operator-supplied.
- Ahub is an optional, agent-neutral rendezvous/mailbox/relay service. Keep its
  server implementation out of the default `anet` and `anet.carriers` import
  surfaces; use explicit `anet.ahub*` modules and the `ahub` optional dependency.
