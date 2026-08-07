# Amesh production hosting

`amesh serve` hosts configured platform adapters and writes bounded events to
the Amesh-owned outbox. It holds one exclusive lock per home, so only one
supervisor may poll a given Discord channel or local spool.

Keep these together in one deployment:

- one Amesh home (`AMESH_HOME` / `--home`);
- the `amesh` package installed into one Python;
- at most one `amesh serve` process for that home.

Amesh is standalone. It does not share a node home, token, identity, ledger,
or service process with Anet or another application.

## systemd (Linux / WSL)

```bash
cd amesh/deploy
./install-amesh-service.sh \
  --python <HOME>/.venv/bin/python \
  --home <HOME>/.config/amesh
```

The installer writes a per-user unit, enables it, and starts it. Diagnostics:

```bash
systemctl --user status amesh-serve.service
journalctl --user -u amesh-serve.service -n 100 --no-pager
amesh --home "$HOME/.config/amesh" adapter list
```

## Windows

```powershell
.\deploy\start-amesh.ps1 -AmeshHome "$env:LOCALAPPDATA\Amesh\homes\default"
.\deploy\status-amesh.ps1 -AmeshHome "$env:LOCALAPPDATA\Amesh\homes\default"
.\deploy\stop-amesh.ps1 -AmeshHome "$env:LOCALAPPDATA\Amesh\homes\default"
```

Persistent hosting uses the `Amesh\Serve` scheduled task:

```powershell
.\deploy\register-amesh-task.ps1 -AmeshHome "$env:LOCALAPPDATA\Amesh\homes\default"
```

The home contains the private identity key, policy and agent databases,
adapter ledgers, logs, lock, and outbox. Do not copy it between deployments.
