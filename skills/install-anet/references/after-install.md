# After the runtime install

Runtime installation does not create a node or authorize trust changes.

For the explicitly authorized WSL host bootstrap, prefer
`../scripts/bootstrap_wsl.py` instead of executing the commands below by hand.
It enforces one host-local Ahub, one private home per Agent, complete-state
validation, and idempotent service/MCP configuration.

## Bind an existing node

Locate the deployment-owned absolute home first. Require both `identity.json`
and `config.json`, then run:

```bash
~/.local/anet/current/venv/bin/anet \
  --home <ABSOLUTE_EXISTING_HOME> doctor
```

If the expected home is missing, stop. Do not initialize a replacement.

## Create a new node

Only when the user explicitly authorizes a new persistent identity:

```bash
~/.local/anet/current/venv/bin/anet \
  --home <NEW_EMPTY_PRIVATE_HOME> init \
  --label <OPERATOR_CHOSEN_LABEL> \
  --host 127.0.0.1 \
  --port <UNUSED_PORT>

~/.local/anet/current/venv/bin/anet \
  --home <NEW_EMPTY_PRIVATE_HOME> doctor
```

Do not copy a home or identity from another host, runtime, profile, or worker.
Pairing and service registration require their own explicit scope.
