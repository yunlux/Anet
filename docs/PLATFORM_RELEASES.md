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
The installer performs a read-only runtime/Ahub preflight first and reports
reuse rather than treating an existing target as a fresh install.

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
The clean entry point runs the bounded runtime/Ahub preflight before any wheel
or virtual-environment mutation.

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
not part of the clean runtime install. It also runs a target/node preflight and
requires `--allow-existing` before adding to an incomplete or alternate
existing Anet root.

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

For a platform-neutral service/device restart check, use the two-phase
`continuity-prepare` / `continuity-verify` interface documented in
[`CONTINUITY_GATE_V1.md`](CONTINUITY_GATE_V1.md). It proves a new supervisor
incarnation, post-prepare sync, and unchanged identity/TLS material; add
`--require-boot-change` only after an actual OS, WSL, or Android boot-session
change. It is narrower than the complete route/store/peer recovery gate.

## Windows automatic deployment prototype

For the requested self-starting Windows behavior, use the separate prototype
deployment entry point:

```powershell
.\scripts\install_windows_oneclick.ps1 `
  -ControlUrl https://example.invalid/anet/control.json
```

It creates one local node, writes the remote control URL, registers a
current-user `Anet\Supervisor` scheduled task, and starts the control client
with an `anet serve` child. For a machine-wide deployment, run an elevated
PowerShell and add `-Admin`; that uses `%ProgramData%\Anet`, the `SYSTEM`
principal, and an `AtStartup` trigger. Use explicit `-Port`, `-LocatorContext`,
and `-Advertise` values when the Windows node will coexist with WSL. See
[`WINDOWS_AUTOSTART.md`](WINDOWS_AUTOSTART.md) for the page shape and current
limitations. Its one-click preflight stops on another known Windows Anet
deployment; `-AllowExisting` is the explicit override.
For direct Windows/WSL connectivity, use distinct ports and a non-loopback
shared host address; a port does not make the two runtimes' `127.0.0.1`
addresses equivalent.

## WSL, Linux, and macOS automatic deployment prototype

The corresponding checkout-free POSIX bootstrap entry points are:

```bash
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform wsl --control-url <CONTROL_URL>
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform linux --control-url <CONTROL_URL>
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform macos --control-url <CONTROL_URL>
```

WSL and non-WSL Linux use `systemd --user`; macOS uses a LaunchAgent. WSL
requires the separate Windows host keepalive bridge when the distribution must
be launched at Windows user logon. See [`POSIX_AUTOSTART.md`](POSIX_AUTOSTART.md)
for service names, locations, diagnostics, and the required systemd
user-session precondition. POSIX one-click preflight stops on another known
same-platform deployment; `--allow-existing` is the explicit override. It also
checks an explicit node-home argument and `ANET_HOME` when either is set, while
runtime-only installation leaves persistent node markers alone.
The macOS installer also verifies the loaded LaunchAgent reports a running
state before it reports a successful deployment. When an Anet checkout is
already available, the direct `scripts/install_*_oneclick.py` entry points
remain equivalent; `bootstrap_posix.py --script-ref <branch-or-tag>` selects
the Git ref used for its temporary helper files. Use
`--repository <HTTPS_GITHUB_REPOSITORY>` as well when those helpers must come
from a fork. The control page's `repo_url` remains the runtime/software source;
it does not select executable bootstrap code.

Windows, WSL, Linux, macOS, and Termux persistent installers share one
versioned stdout interface after successful service registration. The
`anet.deployment.receipt` object separates runtime, node, verified control
source, and supervisor state while retaining platform-specific detail inside
the supervisor Adapter. See [`DEPLOYMENT_RECEIPT_V1.md`](DEPLOYMENT_RECEIPT_V1.md).

## Android Termux automatic deployment prototype

Termux has its own entry point and does not use the Linux systemd entry point:

```bash
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform termux --control-url <CONTROL_URL>
```

It uses Termux-native Python dependencies, `termux-services`/runit, and a
`~/.termux/boot` script. The Termux:Boot add-on must be installed and opened
once by the operator. See [`TERMUX_AUTOSTART.md`](TERMUX_AUTOSTART.md).
