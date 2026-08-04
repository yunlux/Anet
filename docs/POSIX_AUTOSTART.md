# WSL, Linux, and macOS automatic node prototype

On a new device, the checkout-free bootstrap downloads the selected entry point
and shared installer modules into a temporary directory:

```bash
curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform wsl --control-url <CONTROL_URL> \
  --control-key-id community-main \
  --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>
```

Use `--platform linux`, `--platform macos`, or `--platform termux` for the
other POSIX targets. If an Anet checkout is already available, the direct
platform scripts below are equivalent. The clean POSIX installers still
install only a versioned Anet runtime. The explicit one-click deployment
entry points add one persistent node, the remote control client, and the
platform-native auto-start unit:

```bash
# WSL
python3 scripts/install_wsl_oneclick.py \
  --control-url https://example.invalid/anet/control.json \
  --control-key-id community-main \
  --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>

# non-WSL Linux
python3 scripts/install_linux_oneclick.py \
  --control-url https://example.invalid/anet/control.json \
  --control-key-id community-main \
  --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>

# macOS
python3 scripts/install_macos_oneclick.py \
  --control-url https://example.invalid/anet/control.json \
  --control-key-id community-main \
  --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>
```

The control page must contain `software.version` and either an initial
`software.wheel_url` or `software.repo_url` (a top-level `repo_url` is also
accepted), either at the root or in the selected platform overlay. If a wheel
is supplied, `software.sha256` pins it. In unsigned compatibility mode, the
prototype computes the local wheel hash when the declaration is absent; a
pinned/signed page requires `software.sha256` before a wheel is downloaded.
If `--wheel-sha256` is also supplied, it must match the page declaration.
Without a wheel, the
installer passes the repository URL to pip as a Git source, so Git must be
available on the device. The rest of the page uses the same format as the
Windows deployment prototype: default `config`, Peer Cards, `repo_url`,
`pages`, and `kv` JSON sources.
`default_config` is accepted as an alias for `config`, including inside a
platform overlay.
When a repository source is used, optional `software.repo_ref` (or top-level
`repo_ref`) pins a Git branch, tag, or commit for the initial runtime and later
source updates.

After installation:

- WSL and Linux use the current user's `systemd --user` unit
  `anet-supervisor.service`. The installer enables it, starts it immediately,
  and restarts it when reusing an already-active target. It requests user
  lingering when `loginctl` is available. WSL must have
  systemd and a working user manager enabled. The unit starts when the WSL
  distribution is running; systemd alone does not start the WSL distribution
  after a Windows reboot.
- macOS uses the current user's `net.anet.supervisor` LaunchAgent under
  `~/Library/LaunchAgents`. It is loaded immediately, has `RunAtLoad` and
  `KeepAlive` enabled, and the installer verifies `launchctl print` reports
  `state = running` before reporting success.
- Each supervisor runs the remote control client and an `anet serve` child.
  Configuration or Peer Card changes restart the child; an unexpected child
  exit is noticed immediately instead of waiting for the next long control
  page interval. A successful package update re-executes the supervisor from
  the updated runtime.
- A home-level OS lock prevents a second supervisor from operating the same
  node home concurrently.
- Deployment preflight also checks an explicit `--node-home` and the
  `ANET_HOME` environment variable for an existing node home. Runtime-only
  installation does not inspect those persistent-node markers. A node home
  inside the requested runtime boundary is treated as part of that target;
  one outside it stops the deployment unless `--allow-existing` is explicit.
- The final JSON result includes the complete `node.node_id` read from the
  installed CLI's `status` command; identity is never inferred from a path,
  label, host, or port.
- One-shot `anet control-sync` uses the same home lock, so a manual sync cannot
  race the persistent supervisor while it applies config, Peer Cards, or a
  package update.
- Before registering the systemd/LaunchAgent service, the one-click installer
  runs read-only `anet control-verify`. It checks the complete root/nested page,
  local publisher policy, expiry, Peer Cards, and WSL port rules without
  consuming remote-control state; the first supervisor sync still applies the
  page's software update.
- A changed wheel, repository, or repository reference is applied even when its package version is
  unchanged; only the first sync of the already-installed initial version is
  skipped.
- Before a package update, the active Anet package and metadata are snapshotted
  when they belong to the managed runtime. A pip/CLI verification failure
  restores that snapshot; this is local package rollback, not full dependency
  or signed-manifest rollback.
- Each page application snapshots `config.json`, the local signed `card.json`,
  and `peers.json`; a configuration, Card, or software failure restores those
  node-control files before the failed sequence is retried.
- For host-scoped Windows/WSL overlays, the remote-control client rejects equal
  listener ports and loopback locators. Changes to listener or advertised
  address fields regenerate the local signed Card before restart.
- When a page omits `poll_seconds`, the local `remote-control.json` `interval`
  is retained; an explicit page value takes precedence.

Default locations are:

```text
WSL/Linux runtime  ~/.local/anet
WSL/Linux node     ~/.local/anet/nodes/default
macOS runtime      ~/Library/Application Support/Anet
macOS node         ~/Library/Application Support/Anet/nodes/default
```

Before any wheel download or package installation, the POSIX entry points run
the bundled `scripts/install_preflight.py` check. It is bounded to the current
platform: WSL checks WSL paths and user services, Linux checks Linux paths and
user services, macOS checks its Application Support/LaunchAgent paths, and
Termux checks its own `$PREFIX` service tree. The requested target is reused;
another known Anet runtime/deployment in the same platform boundary stops a
one-click install with the detected path. Pass `--allow-existing` only for an
explicit second deployment. Known Ahub data roots and Ahub services/processes
are reported separately and are not started or duplicated by this Anet node
installer. A target-scoped temporary-file lock is acquired before the report,
so concurrent commands cannot both pass preflight and mutate the same runtime
or node deployment. When a WSL node is paired directly with native Windows, use a
non-loopback shared host address and distinct ports; `127.0.0.1` is local to
the runtime and is not a cross-platform locator.

To start the WSL distribution at the current Windows user's next logon and
keep it alive while the Linux user service runs, register the explicit host
bridge from an ordinary Windows PowerShell window:

```powershell
.\\scripts\\register_wsl_keepalive.ps1 `
  -Distribution Ubuntu `
  -LinuxUser <LINUX_USER>
```

This creates `\\Anet\\WSL-KeepAlive` as a current-user logon task. It does not
create a node or copy any identity; it starts `anet-supervisor.service` inside
the selected distribution and holds a small shell process open with no
execution time limit. The task has bounded automatic retries if WSL exits.
The registration command waits up to 30 seconds for the task to enter
`Running` and reports its last task result if startup fails. A
WSL distro is user-scoped, so this bridge intentionally uses the same Windows
user that owns the distro rather than the Windows Anet `SYSTEM` task.

Diagnostics:

```bash
# WSL/Linux
systemctl --user status anet-supervisor.service
journalctl --user -u anet-supervisor.service -n 100 --no-pager

# macOS
launchctl print gui/$(id -u)/net.anet.supervisor
tail -n 100 "$HOME/Library/Application Support/Anet/nodes/default/supervisor.log"
```

The one-click entry points accept `--control-key-id` and
`--control-public-key`. When supplied, the key is written to the node's
`remote-control.json`; the supervisor then requires an Ed25519-signed root
page and every nested `pages`/`kv` page, checks the page expiry, and rejects
signed sequence reuse with different content. Create pages with
`scripts/sign_control_page.py`. Omitting the key retains the unsigned
compatibility bootstrap and is appropriate only for an explicitly trusted
local source. Publisher key rotation and fleet-wide quorum remain deployment
responsibilities.
