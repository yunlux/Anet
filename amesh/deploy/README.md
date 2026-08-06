# Amesh production hosting

`amesh serve` is the persistent runtime that hosts the configured adapters'
background loops. It writes emitted signals to `<home>/amesh-outbound/` and
holds a home-exclusive lock (`amesh-serve.lock`), so a second supervisor for
the same node home fails instead of polling the same ledgers. The lock is an
OS file lock and is released automatically if the process dies.

Keep these together in one deployment:

- one node home (`AMESH_HOME` / `--home`);
- the `amesh` package installed into one Python;
- at most one `amesh serve` process for that home.

Do **not** run `amesh serve` hosting the `discord` adapter on a home where
`anet serve` already hosts the Discord bridge, or the two will poll the same
channel. Use `amesh serve --adapter loopback`, or run Amesh on a home that the
Anet node is not serving, or disable the Anet-hosted bridge.

## systemd (Linux / WSL)

Install a per-user unit that runs the serve loop with restart-on-failure:

```bash
cd amesh/deploy
./install-amesh-service.sh \
  --python <HOME>/.local/anet/venv/bin/python \
  --home <HOME>/.local/anet/nodes/default
```

This substitutes the `@AMESH_PYTHON@` / `@AMESH_HOME@` placeholders in
`amesh-serve.service`, writes `~/.config/systemd/user/amesh-serve.service`, and
enables + starts it. Diagnostics:

```bash
systemctl --user status amesh-serve.service
journalctl --user -u amesh-serve.service -n 100 --no-pager
amesh --home "$HOME/.config/anet" adapter list
```

The unit sets `PYTHONUNBUFFERED=1` and restarts on failure. If the node home is
machine-owned, convert the unit to a system service (`WantedBy=multi-user.target`)
and run it as the owning user or a dedicated account; never point it at a home
another process is serving.

## Windows

The PowerShell helpers in this directory mirror the Anet node scripts:

```powershell
# one-off start / stop / status
.\deploy\start-amesh.ps1 -AmeshHome $env:ANET_HOME
.\deploy\status-amesh.ps1 -AmeshHome $env:ANET_HOME
.\deploy\stop-amesh.ps1 -AmeshHome $env:ANET_HOME
```

Persistent hosting registers an `Amesh\Serve` scheduled task. Current user at
logon by default; run PowerShell as Administrator with `-Admin` to install it
as SYSTEM at startup:

```powershell
.\deploy\register-amesh-task.ps1 -AmeshHome $env:ANET_HOME
.\deploy\register-amesh-task.ps1 -AmeshHome "C:\ProgramData\Anet\nodes\default" -Admin
```

The task restarts a failed serve up to 99 times at one-minute intervals and has
no execution time limit. It launches `start-amesh.ps1`, which records
`amesh.pid` and logs to `amesh.stdout.log` / `amesh.stderr.log` inside the home.

Logs and lock:

```text
<home>/amesh-serve.lock      # exclusive OS lock, one serve per home
<home>/amesh.pid             # Windows launcher PID (Windows only)
<home>/amesh.stdout.log      # Windows stdout
<home>/amesh.stderr.log      # Windows stderr
<home>/amesh-outbound/       # emitted bounded signals
```

A machine restart releases the lock; the task or unit simply starts the next
serve. Diagnose with `status-amesh.ps1` or `systemctl --user status`.
