# Amesh — Agent mesh shell

Amesh is the social shell above Anet. It wraps third-party social platforms
behind one pluggable adapter contract and gives an operator a single
management plane for social evidence, permissions, and observer-local
relationships. Amesh renders projections; it does not become a trust
authority, and it never treats a platform account, score, or label as Anet
identity or capability.

This is a subproject of the Anet repository and reuses the Anet models:

- `anet.social` provides the exact-field signal vocabulary, reputation score,
  confidence, monotonic thresholds, and label namespaces.
- `anet.relations` provides the observer-local Actor / Subject / circle /
  contextual-trust model that every adapter projects into.
- `anet.discord_social` is the first concrete platform bridge, wrapped as the
  `discord` adapter without being rewritten.

## Layout

```text
amesh/
  pyproject.toml
  README.md
  src/amesh/
    __init__.py          # version + public surface
    model.py             # platform-neutral validation and rule types
    policy.py            # operator permission store (SQLite) + evaluation
    adapter.py           # PlatformAdapter contract + shared permission overlay
    signal.py            # bounded social signal model + DirectorySignalSink
    serve.py             # supervisor hosting adapters' background loops
    mcp_server.py        # Amesh management plane as an MCP stdio server
    relations.py         # RelationshipHub over anet RelationshipBook
    adapters/
      __init__.py
      discord.py         # DiscordAdapter wrapping anet.discord_social
      loopback.py        # LoopbackAdapter: local, network-free test platform
  tests/
    test_model.py
    test_policy.py
    test_adapter.py
    test_loopback.py
    test_signal.py
    test_serve.py
    test_mcp.py
    test_cli.py
```

## Concepts

**Adapter** (`PlatformAdapter`): one platform behind a stable contract —
`descriptor`, `status`, `actor`, `set_labels`, `project`, `reply`,
`relation`, `poll_once`, `inject`, `setup`, and `run`. A new platform
implements the contract and registers itself in `builtin_adapter_names()` /
`load_adapter()`; the management plane and relations projection stay generic.
The base class provides the shared operator-permission overlay, so every
adapter applies the same `allow`/`deny` rules without duplicating logic.

**Permission rules** (`amesh policy`): operator rules stored per node home in
`amesh-permissions.sqlite3`. A rule targets an adapter, an actor key (exact or
`*`), an action (`surface`, `reply`, `amplify`, `connect_candidate`, or `*`),
and an effect (`allow` or `deny`). Rules refine — never replace — the Anet
evidence thresholds. `allow` only preserves an action the evidence already
allowed; `deny` removes it. Deny wins on equal specificity; a more specific
`allow` can override a blanket `deny`.

**Relationships** (`amesh relations`): the adapter maps a platform actor key
to its `act_<platform>_<digest>` Actor ID, and `RelationshipHub` reads or
updates the observer-local `anet.relations` book.

## Built-in adapters

**`discord`** wraps `anet.discord_social` unchanged: ingestion, reply gating,
and the private SQLite ledger stay in Anet core; Amesh overlays the permission
gate on the routing callback and on operator replies. Read-only management
commands never need a live bot token.

**`loopback`** is a local, network-free platform used to exercise the same
ingestion → evidence → threshold → permission → projection pipeline without
credentials. Messages are dropped as JSON files into `<home>/loopback-spool/`,
replies are written to `<home>/loopback-outbox/`, and the ledger lives in
`loopback-social.sqlite3`. When `destination_node_id` is set, polled events
that clear the surface threshold (and pass permission rules) are routed as
bounded `social.loopback.signal` objects through the `queue_signal` seam.

**Signals** (`amesh signal`): every routed signal is a bounded, pseudonymous
object (protocol `anet.social.<platform>`): it carries only a content-limited
payload, labels, reputation, decision, and provenance — never a raw platform
identifier or message body. `DirectorySignalSink` writes emitted signals
atomically to `<home>/amesh-outbound/<platform>-<signal_id>.json` with private
permissions, so the shell can store-and-forward without a node.

**`amesh serve`**: hosts every configured, enabled adapter's background loop
in one process. Adapters emit signals through the outbound sink and project
events into the relationship book best-effort. It stops cleanly on SIGTERM /
Ctrl-C. Discord is normally hosted inside `anet serve`; under `amesh serve` it
writes its signals to the outbound directory instead of the node queue.

## Management plane

```text
amesh --home <HOME> adapter list
amesh --home <HOME> adapter status <adapter>
amesh --home <HOME> adapter setup <adapter>          # write default config
amesh --home <HOME> social actor <adapter> <actor_key>
amesh --home <HOME> social label <adapter> <actor_key> --add <label> ...
amesh --home <HOME> social project <adapter> [--limit N]
amesh --home <HOME> social relation <adapter> <actor_key>
amesh --home <HOME> social reply <adapter> <event_key> --text <text>
amesh --home <HOME> social poll <adapter>            # single-shot ingest poll
amesh --home <HOME> social inject <adapter> <author> --text <text> [--channel] [--bot]
amesh --home <HOME> social signals <adapter>         # list outbound signals
amesh --home <HOME> permit <adapter> <actor_key|*> <action|*> allow|deny [--reason ...]
amesh --home <HOME> permit list [<adapter>]
amesh --home <HOME> permit revoke <adapter> <rule_id>
amesh --home <HOME> relations list
amesh --home <HOME> relations circle <subject_ref> <circle> --confidence N --evidence-ref <ref>
amesh --home <HOME> serve [--adapter NAME ...]        # run adapters until stopped
```

`--home` defaults to the deployment-owned `ANET_HOME`. Amesh state
(`amesh-permissions.sqlite3`) lives in the same home as the Anet node and is
never copied between machines.

## MCP

`amesh mcp-server` exposes the management plane as a stdio MCP server over
`AMESH_HOME` (falling back to `ANET_HOME`). Long-lived Agents can read and
manage their own shell:

- read: `amesh_adapters`, `amesh_adapter_status`, `amesh_social_actor`,
  `amesh_social_relation`, `amesh_social_signals`, `amesh_permit_list`,
  `amesh_permit_decisions`, `amesh_relations`;
- mutate locally: `amesh_adapter_setup`, `amesh_social_labels`,
  `amesh_social_poll`, `amesh_social_inject`, `amesh_social_project`,
  `amesh_permit_add`, `amesh_permit_revoke`, `amesh_relations_circle`;
- external effect: `amesh_social_reply` posts to a third-party platform and is
  disabled by default — grant it only to agents that may speak publicly, via
  `AMESH_MCP_ALLOW_REPLY=1`.

Every tool returns JSON and stays inside the observer-local model; none of them
creates an Anet peer, grants a capability, or changes trust.

Example wiring for a gateway (`mcp-stdio.example.json` style):

```json
{
  "amesh": {
    "command": "/path/to/venv/bin/python",
    "args": ["-m", "amesh.cli", "mcp-server"],
    "env": {
      "AMESH_HOME": "/path/to/node-home",
      "AMESH_MCP_ALLOW_REPLY": "0"
    }
  }
}
```

## Production hosting

`amesh serve` is the persistent runtime: it hosts the configured adapters'
background loops, writes emitted signals to `<home>/amesh-outbound/`, and holds
a home-exclusive OS lock (`amesh-serve.lock`) so a second supervisor for the
same node home fails instead of polling the same ledgers. Templates and helpers
live in `amesh/deploy/`:

- **systemd**: `amesh-serve.service` template + `install-amesh-service.sh`
  (per-user unit with restart-on-failure);
- **Windows**: `start-amesh.ps1` / `stop-amesh.ps1` / `status-amesh.ps1` and
  `register-amesh-task.ps1` (scheduled task, current user at logon or SYSTEM at
  startup).

See `amesh/deploy/README.md` for full instructions, diagnostics, and the rule
against running `amesh serve` hosting `discord` on a home that `anet serve` is
already serving.

## Adapter contract details

The `discord` adapter delegates to `anet.discord_social` for ingestion, reply
gating, and the private SQLite ledger. Amesh overlays:

- a permission gate on the routing callback, so a `deny surface` rule stops a
  signal from being queued while the ledger event settles deterministically;
- a permission check before every operator reply;
- a permission-aware `actor` view showing the rules that apply to one actor.

A new platform adapter must never invent its own identity root, social graph,
or authorization. It produces `social.<platform>.signal`-style projections and
records `platform-observed` or `bridge-attested` evidence only.

## Verification

```text
python -m pytest tests
python -m ruff check src tests
```
