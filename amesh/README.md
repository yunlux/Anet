# Amesh

Amesh is an independent social-security middleware for agents and social
platforms. It can host Discord, local test adapters, and future platform
connectors without importing Anet or requiring another project's identity,
packet, trust, or storage model.

## Boundary

```text
Discord / other platforms
            |
      Amesh adapters
            |
  normalize -> pseudonymize -> ledger
            |
   policy + actor permissions
            |
      agent capability grants
            |
   MCP / CLI / local outbox
```

The adapter owns platform API access. The Amesh core owns bounded event
normalization, private ledgers, local relationships, policy decisions, agent
registration, and effect auditing. A social match or platform account never
creates an agent identity, capability, trust relationship, or authorization
outside Amesh.

## External adapters

The core discovers external platform adapters through an in-process registry
and the `amesh.adapters` entry-point group, so Anet, A2A, and future platforms
can live in separate packages without the core importing their models. A
package registers once at import time:

```python
from amesh.adapter import register_adapter
from amesh import PlatformAdapter

class MyPlatformAdapter(PlatformAdapter):
    name = "myplatform"
    # implement descriptor / status / actor / set_labels / project / reply / relation

register_adapter("myplatform", MyPlatformAdapter)
```

```toml
# packaging: entry point alternative
[project.entry-points."amesh.adapters"]
myplatform = "my_adapter:MyPlatformAdapter"
```

`amesh adapter list`, MCP, the connector, and `amesh serve --adapter` all
discover registered adapters automatically.

## Package

The package has no runtime dependency on Anet or another application. The
optional `mcp` extra exposes the management plane; Discord uses Python's
standard library HTTP client.

```powershell
python -m pip install -e .
python -m pip install -e ".[mcp]"
```

Use an Amesh-owned home, for example `~/.config/amesh` or
`C:\ProgramData\Amesh\homes\default`. Set `AMESH_HOME` or pass `--home`.

## Discord

Discord configuration is allowlisted and private. A minimal `discord.json` is:

```json
{
  "version": 1,
  "enabled": true,
  "guild_id": "123456789012345678",
  "channel_ids": ["234567890123456789"],
  "destination_id": "review-agent",
  "token_env": "AMESH_DISCORD_BOT_TOKEN",
  "content_mode": "mentions",
  "poll_interval_seconds": 15,
  "signal_ttl_seconds": 604800,
  "policy": {
    "version": 1,
    "surface": {"min_score": 45, "min_confidence": 0, "required_labels": []},
    "reply": {"min_score": 60, "min_confidence": 25, "required_labels": []},
    "amplify": {"min_score": 72, "min_confidence": 50, "required_labels": []},
    "connect_candidate": {"min_score": 82, "min_confidence": 70, "required_labels": ["relationship:vouched"]}
  }
}
```

The bot token is read from `AMESH_DISCORD_BOT_TOKEN` (or the configured
variable) and is never written to the config or error output. Only configured
guild/channel IDs are polled. Metadata-only events never retain message text;
mention events retain only bounded content.

```powershell
amesh --home <HOME> adapter list
amesh --home <HOME> adapter status discord
amesh --home <HOME> social poll discord
amesh --home <HOME> social actor discord <ACTOR_KEY>
amesh --home <HOME> social reply discord <EVENT_KEY> --agent-id reviewer --text "..."
```

## Agent permissions

Agent bearer tokens are stored only as digests and are returned once during
registration. External effects require both the adapter's evidence/policy
decision and an explicit agent grant.

```powershell
amesh --home <HOME> agent register reviewer "Review agent" --scope observe --scope reply
amesh --home <HOME> agent grant reviewer discord reply allow --reason "review queue"
amesh --home <HOME> agent list
amesh --home <HOME> agent revoke reviewer
```

`operator` is the local CLI/MCP operator identity. MCP replies and discovery
publishing remain disabled by default and require their dedicated capability
environment gates. For a registered agent, set `AMESH_AGENT_TOKEN` for the CLI
and pass `--agent-id`; MCP callers provide the token as the protected
`agent_token` argument.

## Discovery

`amesh.social.discovery` is an Amesh-owned public-safe signal and local feed
matcher. It supports exact topic/capability/language/intent/freshness matches,
tenant isolation, cursor feeds, and immutable feedback. Signals are written to
the Amesh outbox; transport to another agent is an adapter concern.

```powershell
amesh --home <HOME> discovery profile default --topic agent-networking --language zh
amesh --home <HOME> discovery subscribe needs --profile-id default --intent need
amesh --home <HOME> discovery publish --intent need --summary "Need a protocol reviewer"
amesh --home <HOME> discovery feed needs
```

## Connector

`amesh connector serve` runs the token-authenticated agent connector: a local
loopback HTTP boundary at `POST /v1/effects` where an Agent presents its bearer
token and executes a granted effect (currently `reply`) on an adapter. Every
request is recorded in the append-only `amesh-audit.sqlite3`; the token is
authenticated, the adapter/action grant is required, and the audit shows only
agent IDs and outcomes, never tokens.

```powershell
amesh --home <HOME> connector serve --adapter loopback --port 8765
amesh --home <HOME> connector audit --limit 100
```

## Routes

`amesh.social` signals are queued into a durable route/outbox state machine
(`amesh-routes.sqlite3`): one route per (destination, signal_id), so a re-polled
signal is deduplicated. `amesh serve` delivers due routes to the outbound
directory with exponential-backoff retries, marks signals expired past their TTL,
and fails routes after `MAX_ATTEMPTS`. Destination/adapter policy rules deny a
route before it is stored.

```powershell
amesh --home <HOME> route status
amesh --home <HOME> route list --state failed
amesh --home <HOME> route retry <route_id>
amesh --home <HOME> route flush
amesh --home <HOME> route policy <destination> <adapter> deny
amesh --home <HOME> route policy-list
```

## Management

`amesh serve` owns one exclusive home lock and hosts configured adapters. The
stdio MCP server exposes read operations and local mutations; platform replies
and discovery publishing require explicit process capability gates. Durable
private state lives under the Amesh home, never under an Anet node home.

See [`docs/AMESH_STANDALONE_ARCHITECTURE.md`](../docs/AMESH_STANDALONE_ARCHITECTURE.md),
[`docs/AMESH_DISCOVERY_V1.md`](../docs/AMESH_DISCOVERY_V1.md), and the adapter
source under `src/amesh/adapters/` for the current protocol and security
details.
