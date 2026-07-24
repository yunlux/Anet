# Clean platform installs and deployment releases

Anet uses one protocol, one Python package and one version on Windows, WSL and
macOS. The default platform install is deliberately independent from every
Agent runtime:

- it installs only a versioned Anet virtual environment;
- it does not create an identity or `ANET_HOME`;
- it does not inspect Hermes or any other Agent runtime;
- it does not assume a profile count, label, service name, or supervisor;
- it does not register or start a node service.

Node creation and Agent integration are later, explicit deployment actions.
For an Agent-operated CLI/MCP workflow, continue with
[`CLI_AGENT_GUIDE.md`](CLI_AGENT_GUIDE.md) and
[`MCP_AGENT_GUIDE.md`](MCP_AGENT_GUIDE.md).

Every installer accepts a feature set:

- `core`: CLI and protocol runtime only;
- `mcp`: core plus the stdio MCP dependency;
- `full`: MCP plus Ahub server and Relay dependencies.

Use `-Feature mcp` on Windows or `--feature mcp` on WSL/macOS for an
Agent-ready CLI+MCP runtime. Feature variants use separate version directories,
so installing MCP does not mutate an existing core runtime.

## Clean Windows install

```powershell
.\scripts\install_windows.ps1 `
  -Version 0.12.1 `
  -Wheel .\dist\anet_fabric-0.12.1-py3-none-any.whl `
  -WheelSha256 <PINNED_SHA256>
```

The default root is `%LOCALAPPDATA%\Anet`. Each release lives under
`versions\<version>\venv`; `current.json` identifies the selected runtime.
An explicit `-Root` may be used for managed installations.

## Clean WSL install

```bash
python3 scripts/install_wsl.py \
  --version 0.12.1 \
  --wheel dist/anet_fabric-0.12.1-py3-none-any.whl \
  --wheel-sha256 <PINNED_SHA256>
```

The default root is `~/.local/anet`, with versioned environments under
`versions/<version>/venv` and an atomic `current` symlink. This location is an
Anet platform runtime, not a Hermes profile or node home.

## Clean macOS install

```bash
python3 scripts/install_macos.py \
  --version 0.12.1 \
  --wheel dist/anet_fabric-0.12.1-py3-none-any.whl \
  --wheel-sha256 <PINNED_SHA256>
```

The default root is `~/Library/Application Support/Anet`, using the same
versioned layout and atomic `current` pointer as WSL. The existing
`bootstrap-macos.sh` remains a separate, optional node-onboarding helper; it is
not part of the clean runtime install.

## Existing deployment release gates

These gates are optional deployment tools, not default installers:

- `scripts/windows_release_gate.ps1` upgrades an explicit Windows venv and
  explicit, stopped node homes.
- `scripts/wsl_release_gate.py` upgrades one explicit WSL node/service.
- `scripts/wsl_multi_node_release_gate.py` atomically upgrades multiple
  explicit WSL node/service pairs that use one platform runtime.

Every node home and service must be supplied by the deployment owner. The
multi-node gate does not know Hermes, profile names, Agent roles, or default
paths:

```text
--deployment /absolute/node/home=systemd-unit.service
```

Release gates may back up and verify existing identities. Clean installers
never read them. Neither layer creates, copies, renames, or infers a persistent
node identity.
