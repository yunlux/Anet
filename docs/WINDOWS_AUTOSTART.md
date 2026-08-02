# Windows automatic node prototype

The clean Windows installer installs only a versioned runtime. The prototype
deployment installer adds the behavior needed for a self-starting device:

1. install the runtime from a remote JSON control page;
2. create one local Anet node home and identity;
3. write the control-page URL into that node home;
4. register an `Anet\\Supervisor` scheduled task;
5. start a supervisor that runs the remote control client and an `anet serve`
   child process.

The supervisor holds an OS-level lock inside the node home, so a second
supervisor for the same home exits instead of competing for updates or the
listener port.
The one-shot `anet control-sync` command uses the same lock, so it cannot race
the persistent supervisor while applying configuration, Peer Cards, or a
package update.

Run it from a checkout that contains the Windows scripts:

```powershell
.\\scripts\\install_windows_oneclick.ps1 `
  -ControlUrl https://example.invalid/anet/control.json
```

The default is a current-user installation. It uses `%LOCALAPPDATA%\\Anet`,
an `AtLogOn` trigger, and the interactive user as the task principal. To
install for the whole machine, open PowerShell with **Run as administrator**
and add `-Admin`:

```powershell
.\\scripts\\install_windows_oneclick.ps1 `
  -Admin `
  -ControlUrl https://example.invalid/anet/control.json
```

Administrator mode uses `%ProgramData%\\Anet`, creates the default node under
`%ProgramData%\\Anet\\nodes\\default`, and registers the same task as the
`SYSTEM` account with an `AtStartup` trigger and highest run level. The node
therefore starts at machine boot without requiring a user to log in. Pass
`-Root` or `-NodeHome` to select an explicit machine-owned location. Pass
`-Port`, `-ListenHost`, `-LocatorContext`, and repeated `-Advertise` values to
make the node's address stable and reachable by a same-host WSL node.
The task is also configured to restart a failed supervisor up to 99 times
with a one-minute interval and has no execution time limit. The initial wheel
uses `software.sha256` from the control page when present; if it is omitted,
the installer records the locally observed hash instead.
When the installer explicitly reuses this target, it stops the managed task
and waits for the old supervisor to release the node lock before starting the
new task, so updated control-page settings take effect immediately.
After registration it waits up to 30 seconds for the task to enter `Running`;
a missing start is reported with the task's last result instead of being
reported as a successful install.

Every Windows entry point runs a read-only preflight before it downloads a
wheel, creates a virtual environment, or registers a task. The clean runtime
installer checks the selected runtime roots and known Ahub data roots. The
one-click installer additionally checks native Windows Anet roots,
`Anet\\Supervisor` tasks, Anet/Ahub services, and matching processes. An
existing target is reused; another known Windows deployment stops the install
with its path instead of silently creating a second node. Use
`-AllowExisting` only when an operator explicitly wants that second deployment.
Each entry point also takes a target-scoped OS mutex before this preflight, so a
concurrent install command cannot pass the same report and create a duplicate
runtime or task during the same race window.
The check is native-Windows scoped: it does not inspect WSL, whose Linux user
home, node identity, and service manager are independent. Ahub findings are
reported but this installer does not start a new Ahub merely because the
optional `full` feature is selected. The separate `Anet\WSL-KeepAlive` host
bridge task is also excluded from duplicate-node detection; it only starts the
WSL user service and does not own a Windows node home.

For a Windows/WSL pair on mirrored networking, use distinct ports and the same
opaque host zone, for example:

```powershell
.\\scripts\\install_windows_oneclick.ps1 `
  -Admin `
  -ListenHost 0.0.0.0 `
  -Port 43111 `
  -LocatorContext host:REPLACE_WITH_SHARED_HOST_ZONE `
  -Advertise "tls://REPLACE_WITH_SHARED_HOST_ADDRESS:43111?scope=host&zone=REPLACE_WITH_SHARED_HOST_ZONE&priority=0" `
  -ControlUrl https://example.invalid/anet/control.json
```

The WSL command should bind a non-loopback interface, use another port such as
`--port 43112`, advertise the same shared host address, and use the same
`host:` context. Port separation prevents listener collisions; it does not
make Windows and WSL `127.0.0.1` interchangeable. A host-scoped locator must
not advertise `127.0.0.1`, `localhost`, or `::1`, because the receiving runtime
can connect to its own loopback and then report a TLS/Node ID mismatch.

The same rule is enforced again whenever the supervisor applies a remote
configuration: enabled Windows/WSL overlays must either both be local-only or
both declare host scope; host-scoped overlays with equal listener ports are
rejected. A changed listener, advertised locator, context, or capability
regenerates the local signed `card.json` before the `anet serve` child is
restarted.

For example, the WSL side uses the same `REPLACE_WITH_SHARED_HOST_ADDRESS`
with port `43112` and `--listen-host 0.0.0.0`. Replace the placeholder with an
address or hostname reachable from both runtimes and allow the two ports in
the appropriate host firewall.

The same script can be downloaded and invoked from PowerShell when the control
page contains `software.repo_url` (a GitHub repository is the default source
for the helper scripts):

```powershell
& ([scriptblock]::Create((Invoke-RestMethod `
  https://raw.githubusercontent.com/yunlux/Anet/main/scripts/install_windows_oneclick.ps1))) `
  -Admin `
  -ControlUrl https://example.invalid/anet/control.json
```

The downloaded command above must also run from an elevated PowerShell when
`-Admin` is used.

The control page needs `software.version` and either a
`software.wheel_url` or `software.repo_url` for the initial runtime
installation, either in the common `software` object or in the selected
`platforms.windows.software` overlay. A top-level `repo_url` is also accepted.
When a wheel is supplied, `software.sha256` pins it. When only `repo_url` is
provided, the runtime installer passes the repository to pip as a Git source,
so Git must be available on the device. The same `repo_url` is used for
matching helper scripts and subsequent source-based updates.
Optional `software.repo_ref` (or top-level `repo_ref`) pins the helper scripts,
initial runtime, and later source updates to one Git branch, tag, or commit.

The initial installer accepts `config` or its equivalent `default_config`
object, including the selected platform overlay. The running remote-control
client uses the same precedence, so initial deployment and later sync do not
interpret the page differently.

The page may contain a `platforms` object with `windows`, `wsl`, `linux`,
`macos`, or `termux` overlays. The selected overlay is merged after the common
document, so one control URL can share common software and community peers
while overriding the wheel, hash, repository reference, listen ports, and advertised addresses for
one platform. See
[`windows-control-page.example.json`](windows-control-page.example.json).

The first implementation intentionally uses plain JSON pages. A page can
contain a software package, default node configuration, direct Peer Cards,
and nested page/KV URLs:

See the copyable file [`windows-control-page.example.json`](windows-control-page.example.json)
for the same shape.

`pages` and `kv` are equivalent lists of JSON URLs. A KV provider can expose
one JSON object at a stable key URL; the supervisor follows those URLs and
merges the returned declarations.

The local `remote-control.json` `interval` is used when the composed page and
its child pages omit `poll_seconds`; an explicit page value overrides it. This
keeps a device's polling cadence configurable without changing the page.

`repo_url` is recorded as the advertised project source. When `repo_ref` is
present, it is recorded and passed to Git for the initial and subsequent source
installations. A package update is
performed from `wheel_url` when present; otherwise the prototype passes the
repository URL to `pip` as a `git+` package source. The active virtual
environment is updated in place and the supervisor re-executes itself after a
successful update. The first sync records an already-installed matching
version without reinstalling it; a later changed wheel/repository is applied
even when its package version is unchanged. Before the update, package files
and metadata inside the managed runtime are snapshotted; a pip or CLI
verification failure restores them. This local rollback does not replace a
signed manifest, dependency rollback, or publisher policy.

Each page application also snapshots `config.json`, the local signed
`card.json`, and `peers.json`; a configuration, Card, or software failure
restores those node-control files before the failed sequence is retried.

The supervisor keeps the last local configuration when a page is unavailable.
Use one bounded sync with:

```powershell
anet --home $env:ANET_HOME control-sync --url .\\control.json --no-software
```

For diagnostics, inspect:

```text
<ANET_HOME>\\remote-control.json
<ANET_HOME>\\remote-control-state.json
<ANET_HOME>\\supervisor.log
%LOCALAPPDATA%\\Anet\\current.json
%ProgramData%\\Anet\\current.json     # administrator mode
```

This page format is a functional bootstrap prototype, not yet a production
trust protocol. In particular, the current implementation accepts unsigned
JSON, applies remote configuration, imports Peer Cards, and can install a
remote wheel or Git repository. Do not use it as a public update channel until
the signed manifest, publisher quorum, rollback, and local policy gates are
implemented.
