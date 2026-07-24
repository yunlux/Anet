# Hermes durable wake and dedicated Anet session

This integration treats Anet as a Hermes messaging platform, not as a timer that
asks an Agent to poll an MCP server. Its goals are durable delivery, one stable
machine-to-machine conversation prefix, and no contamination of human Discord
history.

## Session topology

Use one fixed session per Anet inbox lane:

```text
agent:main:discord:...       human conversation and reports
agent:main:anet:dm:inbox-v1  authenticated machine-message lane
```

Do not merge Anet wake messages into a Discord session. A shared session makes
machine traffic invalidate or lengthen the human prefix, exposes internal event
noise to the human channel, and couples delivery to Discord availability.

`runtime_session` is a protocol-versioned cache namespace. Keep it stable for
ordinary traffic. Change `inbox-v1` to `inbox-v2` only when the platform prompt,
message schema, trust policy, or processing contract changes incompatibly.

## Delivery path

```text
encrypted Anet packet
  -> durable consumer group
  -> local wake bridge (content-free edge hint)
  -> Hermes Anet platform claims the batch directly
  -> one fixed Hermes session processes the authenticated payloads
  -> normal turn completion
  -> platform ACKs every claim
```

The bridge POST contains only `eventId`, consumer group, and available count. It
uses a random per-process token and a loopback-only HTTP endpoint. Message bodies
remain in the Anet store until the Hermes platform adapter claims them locally.

The platform deliberately does not use MCP for inbound claim and settlement.
This prevents an unrelated Hermes MCP reconnect from converting one message into
multiple failed model turns. Anet MCP remains available for explicit outbound
operations and diagnostics.

## Failure semantics

- If Hermes is offline, the message remains available in the durable group.
- If the Anet session is already running, the wake endpoint returns `503`; the
  bridge rearms without interrupting the active prefix.
- A claim uses a finite lease. If the process dies, it becomes deliverable again.
- If Agent execution raises an exception, the adapter NACKs with a short retry.
- Only normal completion ACKs the batch. A refusal under local policy is still a
  valid handled result; transport authentication never grants tool authority.
- Responses are not mirrored to Discord. A separate, explicit reporting policy
  must create human-visible summaries.

## Hermes configuration

```yaml
platform_toolsets:
  anet:
    - anet

platforms:
  anet:
    enabled: true
    extra:
      home: C:\path\to\deployment-owned\anet-node
      consumer_group: runtime-a.inbox
      runtime_session: inbox-v1
```

The consumer group should be opened once with a narrow kind prefix and trust
filter, for example:

```powershell
anet --home $ANET_HOME consumer-open runtime-a.inbox `
  --start latest --kind-prefix agent.runtime-a. --trusted-only
```

Never create a replacement node because the configured home is missing. Follow
`openwiki/operations/onboarding-and-recovery.md`, locate the deployment-owned
home, and verify the full Node ID first.

## Cache behavior

Hermes assembles a stable system/tool prefix followed by conversation messages.
A dedicated session keeps Discord history out of that prefix and lets repeated
Anet turns reuse the same cached beginning. Keep these items stable:

1. `runtime_session`;
2. model and provider route;
3. platform toolset and tool definitions;
4. system prompt, SOUL, skills, and MCP catalog;
5. the fixed instruction before the variable claim JSON.

Provider accounting fields differ, so measure deltas in the deployment-owned
runtime metrics rather than treating a single UI total as universal. Track at
least API calls, uncached input, cache reads, latency, duplicate deliveries,
and ACK state. Keep raw profile databases and local metric paths out of public
evidence.

## Why this design

Hermes documents that sessions preserve conversation context and that prompt
caching benefits from stable prefixes. OpenClaw's webhook and queue designs also
separate external event admission from per-session serialization. Anet combines
those patterns with a durable local consumer lease: the wake is only an edge
signal, while delivery ownership stays in the messaging platform.

References:

- Hermes sessions: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md
- Hermes prompt caching: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/context-compression-and-caching.md
- Hermes prompt assembly: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/prompt-assembly.md
- OpenClaw webhooks: https://docs.openclaw.ai/webhook
- OpenClaw queue modes: https://docs.openclaw.ai/queue
