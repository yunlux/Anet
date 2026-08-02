# Discord Social Bridge v1

Status: implemented for the WSL Anet runtime. The bridge is disabled until an
operator creates `discord-social.json` under the deployment-owned `ANET_HOME`.
It runs inside the existing `anet serve` process and does not create a second
node, copy an identity, or treat a Discord account as an Anet peer.

## Purpose and boundary

Discord is Anet's first human-facing social discovery adapter:

```text
explicitly allowed Discord channel
  -> metadata / bot-mention projection
  -> local pseudonymous evidence ledger
  -> labels + reputation score + confidence
  -> threshold decision
  -> public-safe social.discord.signal
  -> already trusted Anet destination
```

This adopts EigenFlux's signal/feed idea but keeps discovery separate from
authorization. It also follows Matrix's event/state split: the Discord message
snapshot is an immutable event; actor counters, labels and current reputation
are derived local state.

The bridge never:

- turns a Discord user, role, Guild, username or score into an Anet Node ID;
- adds a Peer Card, grants a capability, signs a human approval, or executes a
  tool;
- uploads attachments, embeds or arbitrary channel history into Anet;
- stores a bot token in `config.json`, `discord-social.json`, SQLite, logs or
  CLI output;
- starts another process against the same private node home.

`connect_candidate` means only “consider an out-of-band identity proof and
explicit pairing.” It is not a trust result.

## Actor and Subject projection

After the source event is durable, the running Node also projects the event
into its private `relationships.json` model:

- the existing HMAC `actor_key` is combined with the observing Node namespace
  to derive an opaque `act_discord_<digest>` Actor ID;
- the Actor kind is `account.discord`;
- a local REST observation receives `platform-observed` proof scope;
- a recipient of `social.discord.signal` records only `bridge-attested`, with
  the signed sending Node as issuer;
- every new Actor begins with a separate, revisable Subject hypothesis.

A mention or reply may move the fresh hypothesis from `public` to `known`.
It cannot reactivate an observer-local relationship that is already `dormant`
or `ended`; that requires an explicit `relation-circle` action.
Nothing in Discord score, labels, volume, or bridge trust creates a closer
circle, contextual trust, PeerBook entry, capability, or authorization. The
relationship file receives event references and coarse facets only; raw
Discord snowflakes, pseudonym keys, message content, Guild/channel keys and
scores remain outside it.

The relationship advisor deliberately ignores Discord reputation, account age,
message volume, reactions and platform labels. Discord-only activity therefore
cannot produce a `collab` or contextual-trust suggestion. A later verified Anet
task exchange with the same Subject hypothesis may independently satisfy the
narrow task-evidence policy.

## Discord permissions and content access

Create a dedicated Discord application/bot with only the permissions needed on
the selected channels:

- View Channel;
- Read Message History;
- Send Messages when threshold-gated replies are wanted.

Do not grant Administrator, Manage Guild, Manage Roles, Manage Webhooks, or
member-list access. v1 uses bounded REST polling rather than a Gateway
connection. The default `mentions` content mode retains message content only
when the bot is explicitly mentioned; every other event becomes metadata-only.
Discord documents bot mentions as an exception to the privileged Message
Content restriction. `metadata` mode retains no message content at all.

The client verifies each configured channel belongs to the configured Guild
before ingesting it. It follows Discord's `Retry-After` response on HTTP 429 and
does not hard-code platform rate limits.

Official references:

- [Discord message resource and allowed mentions](https://docs.discord.com/developers/resources/message)
- [Gateway intents and Message Content](https://docs.discord.com/developers/events/gateway)
- [Discord API rate limits](https://docs.discord.com/developers/topics/rate-limits)

## Local privacy boundary

The source ledger lives beside the existing node state:

- `discord-social.json`: non-secret allowlist, destination and policy;
- `discord-social.key`: random `0600` HMAC key used only for local pseudonyms;
- `discord-social.sqlite3`: local event, evidence, label, cursor and outbound
  reply ledger.

Discord user, Guild and Channel snowflakes are never put into
`social.discord.signal`. The signal carries HMAC-derived `actor_key`,
`guild_key`, `channel_key` and `source_event_id`. Mention content is bounded to
2,000 characters. Attachments are represented only by
`content:attachment`; attachment names, URLs and bodies are not copied.

The local source database necessarily retains channel/message IDs so an
operator-approved reply can target the original event. Protect the whole
deployment-owned node home as private state.

## Scoring and confidence

Reputation is deliberately two-dimensional:

- `raw_score`: bounded evidence balance around a neutral 50;
- `confidence`: amount and strength of evidence;
- `score`: raw score shrunk back toward 50 when confidence is low.

Automated Discord activity is capped so repeated mentions or reactions cannot
manufacture unlimited trust. Operator labels have explicit, reviewable
weights. The default thresholds are:

| Action | Minimum score | Minimum confidence | Extra condition |
|---|---:|---:|---|
| `observe` | 0 | 0 | always |
| `surface` | 45 | 0 | no blocking risk label |
| `reply` | 60 | 25 | bot was mentioned; actor is not bot/webhook |
| `amplify` | 72 | 50 | actor is not bot/webhook |
| `connect_candidate` | 82 | 70 | `relationship:vouched` |

`risk:block`, `risk:spam`, `risk:impersonation`, and `risk:malware` reduce the
event to `observe` regardless of score. Thresholds must be monotonic.

Operator-owned label namespaces are:

```text
community:* interest:* language:* relationship:* risk:* status:*
```

Adapter-owned namespaces such as `actor:*`, `content:*`, `interaction:*`,
`platform:*` and `event:*` cannot be forged through the label CLI. In
particular, `event:pinned` and `event:mass-mention` are derived only from the
allowlisted Discord event.

## WSL configuration

First deploy the code through the normal pinned WSL release gate. Do not create
a new node or copy any node-home file. On the existing WSL node, configure one
Guild, one or more explicit channels, and optionally one already trusted Anet
destination:

```bash
anet --home "$ANET_HOME" discord-social-config \
  --guild '<DISCORD_GUILD_SNOWFLAKE>' \
  --channel '<DISCORD_CHANNEL_SNOWFLAKE>' \
  --destination '<ALREADY_TRUSTED_DESTINATION_NODE_ID>' \
  --content-mode mentions \
  --poll-seconds 15
```

`--destination` is rejected unless it is already in the local PeerBook. Omitting
it keeps the ledger local and performs no Anet routing.

Place the token in the environment file consumed by the existing service:

```bash
install -d -m 700 "$HOME/.config/anet"
install -m 600 /dev/null "$HOME/.config/anet/discord-social.env"
read -rsp 'Discord bot token: ' ANET_DISCORD_TOKEN
printf 'ANET_DISCORD_BOT_TOKEN=%s\n' "$ANET_DISCORD_TOKEN" \
  > "$HOME/.config/anet/discord-social.env"
unset ANET_DISCORD_TOKEN
```

The repository unit template uses:

```ini
EnvironmentFile=-%h/.config/anet/discord-social.env
```

After an approved deployment/restart, `anet serve` loads the bridge in the same
runtime that owns the identity. A missing token, invalid Guild/channel scope,
unknown destination, malformed config, or non-monotonic policy fails closed.

Inspect only redacted status:

```bash
anet --home "$ANET_HOME" discord-social-status
```

The status omits Guild IDs, Channel IDs, user IDs, event content and the token.

If relationship projection was unavailable after the Discord event had already
been persisted, stop the owning runtime and replay the durable metadata:

```bash
anet --home "$ANET_HOME" discord-social-project --limit 1000
```

The replay is idempotent and cannot create Anet peer trust or authorization.

## Labels, review and replies

Signals delivered to the destination include a pseudonymous `actor_key` and
`source_event_id`. Inspect or label the actor on the source WSL node:

```bash
anet --home "$ANET_HOME" discord-social-actor '<ACTOR_KEY>'
anet --home "$ANET_HOME" discord-social-label '<ACTOR_KEY>' \
  --add 'interest:agent-infrastructure' \
  --add 'relationship:vouched' \
  --source operator
```

An outbound reply is explicit and bound to one stored event:

```bash
anet --home "$ANET_HOME" discord-social-reply '<EVENT_KEY>' \
  --text 'Thanks — I am interested in that design.'
```

The reply is rejected unless the current actor evidence and original event pass
the `reply` threshold. The REST request sets `allowed_mentions.parse=[]`,
does not ping the replied user, binds the original message with
`fail_if_not_exists`, and uses an enforced nonce. A second different reply for
the same event is rejected.

## Delivery and failure semantics

- The Discord cursor advances only after the local event is persisted and any
  required Anet signal is queued.
- Relationship projection is best-effort after durable ingest. A projection
  failure cannot discard the Discord event or reject an otherwise valid Anet
  Packet; the replay command can recover it.
- If Anet queueing fails, the cursor remains behind; the next poll reuses the
  existing event without incrementing reputation twice.
- A crash after Anet queueing but before local route settlement can create a
  second encrypted Packet. Both carry the same stable `signal_id`, so consumers
  must deduplicate semantically. Delivery is at-least-once, not exactly-once.
- Discord edits are not yet backfilled by the REST polling slice. v1 records
  the observed create snapshot; Gateway edit/delete/reaction events are a later
  extension.
- A reply reservation is durable and content-bound. Discord's enforced nonce
  closes the immediate retry window, but a downstream API and SQLite are not
  one atomic transaction.

## Remaining gates

1. Run against a dedicated test Guild/channel with a non-production bot token.
2. Verify WSL restart, token rotation, HTTP 429, revoked channel permission and
   Anet destination outage behavior.
3. Add signed operator feedback events instead of relying only on mutable local
   labels.
4. Add a versioned `SignalProfile/Subscription` matcher only after the direct
   Discord-to-known-Agent loop has useful precision measurements.
5. Evaluate Gateway event support for edits, deletes and reactions without
   requesting broader privileged intents by default.
