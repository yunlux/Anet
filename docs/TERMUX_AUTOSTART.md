# Android Termux automatic node prototype

Termux can run the Anet core node, but it is not ordinary Linux: it has no
systemd user manager. The Termux deployment uses Termux-native Python packages,
`termux-services`/runit for supervision, and Termux:Boot for boot-time startup.

Prepare Termux once, then run the installer from an Anet checkout:

```bash
pkg update
pkg install -y python python-pip git

python3 scripts/install_termux_oneclick.py \
  --control-url https://example.invalid/anet/control.json
```

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

The installer additionally installs `python-cryptography`, `python-msgpack`,
and `termux-services`, then:

1. installs the initial Anet wheel into `~/.local/anet` using Termux's native
   compiled dependencies;
2. creates one node under `~/.local/anet/nodes/default`;
3. creates, enables, and restarts the `anet-supervisor` runit service when an
   existing target is reused;
4. writes `~/.termux/boot/start-anet-services` for boot startup.

Install and open the Termux:Boot add-on once. It must come from the same
distribution/signing source as Termux. On the next Android boot, the script
acquires a wake lock when available, starts `termux-services`, and brings up
the enabled Anet supervisor. The add-on's official instructions require the
app to be opened once and boot scripts to live under `~/.termux/boot/`.

Useful diagnostics:

```bash
sv status anet-supervisor
sv up anet-supervisor
tail -f "$PREFIX/var/log/sv/anet-supervisor/current"
cat "$HOME/.local/anet/nodes/default/remote-control-state.json"
```

The default `core` feature is the recommended phone mode. `--feature mcp` is
available, but consumes more memory and its optional dependencies are installed
from PyPI inside the runtime environment. Android vendors may still kill
background processes or remove wake locks; battery-optimization exemptions may
be necessary for a node expected to stay online. The phone is normally an
outbound/reachable edge node rather than a public Internet listener.

This is the same unsigned remote-control bootstrap prototype as the other
platforms. Signed manifests, publisher quorum, rollback, and local policy
gates are still required before using a public update channel.
