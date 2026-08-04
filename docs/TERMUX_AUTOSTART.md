# Android Termux automatic node prototype

Termux can run the Anet core node, but it is not ordinary Linux: it has no
systemd user manager. The Termux deployment uses Termux-native Python packages,
`termux-services`/runit for supervision, and Termux:Boot for boot-time startup.

Prepare Termux once. A checkout-free installation only needs Python and curl;
the bootstrap downloads the platform entry point and shared installer modules
to a temporary directory:

```bash
pkg update
pkg install -y python curl

curl -fsSL https://raw.githubusercontent.com/yunlux/Anet/main/scripts/bootstrap_posix.py | \
  python3 - --platform termux \
  --control-url https://example.invalid/anet/control.json \
  --control-key-id community-main \
  --control-public-key <BASE64URL_ED25519_PUBLIC_KEY>
```

When an Anet checkout is already present, the direct entry point
`python3 scripts/install_termux_oneclick.py` remains equivalent.
The installer itself installs the remaining Termux packages, including Git
when a repository source is selected.

The installer performs its read-only duplicate check before it runs `pkg`.
It reuses the target runtime/node, reports known Ahub data roots, and stops if
another Termux Anet deployment or supervisor is already present. Use
`--allow-existing` only when a second explicit Termux deployment is intended.
It also acquires a target-scoped install lock before that check, so two
concurrent Termux commands cannot create the same runtime or supervisor twice.

The selected Termux platform overlay can set `software`, `listen_host`,
`listen_port`, `advertise`, and `locator_contexts`; a platform `software`
object can override the common wheel URL and hash. The installer applies
those values before enabling runit and re-signs the local Card. If a node
already exists, an explicitly requested host or port must match it.
When the install command pins a control publisher, `software.sha256` is
required for a wheel; a signed `repo_url` is the explicit source-install
alternative. Missing hashes are only tolerated by the unsigned compatibility
bootstrap. If `--wheel-sha256` is also supplied, it must match the page's
`software.sha256` when both are present.

The installer additionally installs `python-cryptography`, `python-msgpack`,
and `termux-services`, then:

1. installs the initial Anet wheel into `~/.local/anet` using Termux's native
   compiled dependencies;
2. creates one node under `~/.local/anet/nodes/default`;
3. creates, enables, and restarts the `anet-supervisor` runit service when an
   existing target is reused;
4. writes `~/.termux/boot/start-anet-services` for boot startup.

Before enabling the runit service, the installer runs read-only
`anet control-verify`; it checks the complete control page and local publisher
policy without consuming the first supervisor sync or its software update.
The final stdout object uses the shared `anet.deployment.receipt` v1 interface.
Runit state is under `supervisor`, while the boot script and Termux:Boot
prerequisite remain under `platform_details`; see
[`DEPLOYMENT_RECEIPT_V1.md`](DEPLOYMENT_RECEIPT_V1.md). The installer waits for
a fresh supervisor/child observation before emitting success.

Install and open the Termux:Boot add-on once. It must come from the same
distribution/signing source as Termux. On the next Android boot, the script
acquires a wake lock when available, starts `termux-services`, and brings up
the enabled Anet supervisor. The add-on's official instructions require the
app to be opened once and boot scripts to live under `~/.termux/boot/`.

Useful diagnostics:

```bash
anet --home "$HOME/.local/anet/nodes/default" supervisor-status
sv status anet-supervisor
sv up anet-supervisor
tail -f "$PREFIX/var/log/sv/anet-supervisor/current"
cat "$HOME/.local/anet/nodes/default/remote-control-state.json"
```

See [`SUPERVISOR_HEALTH_V1.md`](SUPERVISOR_HEALTH_V1.md) for the common health
exit and staleness semantics.

The default `core` feature is the recommended phone mode. `--feature mcp` is
available, but consumes more memory and its optional dependencies are installed
from PyPI inside the runtime environment. Android vendors may still kill
background processes or remove wake locks; battery-optimization exemptions may
be necessary for a node expected to stay online. The phone is normally an
outbound/reachable edge node rather than a public Internet listener.

When `--control-key-id` and `--control-public-key` are supplied, the installer
writes the pinned Ed25519 key into `remote-control.json`. The supervisor then
requires signed root and nested `pages`/`kv` control pages, checks expiry, and
rejects signed sequence reuse with different content. Use
`scripts/sign_control_page.py` on the publisher side. Omitting the key retains
the unsigned compatibility bootstrap and should be limited to an explicitly
trusted local source; publisher rotation and fleet-wide quorum remain outside
the Termux service.
