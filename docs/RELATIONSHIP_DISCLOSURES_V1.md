# Relationship Disclosures v1

Relationship disclosures let one Anet Actor show a selected, content-free
slice of its observer-local relationship activity to exactly one pinned peer.
They support guardian, companion, operator, and peer observation without
creating a shared social graph.

## Object and transport

The Packet kind is:

```text
social.relationship.disclosure
```

Its MessagePack body has type `anet.relationship.disclosure.v1` and contains:

- the observer and audience Actor IDs;
- a deterministic `rdis_` content digest;
- issue time and opaque next cursor;
- one to 100 activities in observer-local append order;
- explicit `content-free`, `audience-private`, and
  `authorization_effect: none` boundaries.

The normal Anet Packet provides sender authentication, destination binding,
end-to-end encryption, TTL, routing, optional one-time-prekey forward secrecy,
deduplication, and receipts. The disclosure body repeats observer and audience
IDs so the runtime can reject a valid body replayed toward another destination.

## Privacy projection

Activities may contain only bounded structural fields already produced by the
relationship activity feed: opaque Actor/Subject/event identifiers, categories,
fact levels, timestamps, circle or confidence estimates, interaction facets,
transition references, and explicit decisions. Evidence references are digests.

The disclosure schema rejects unknown detail fields, raw text, task objectives
or results, filenames, credentials, Actor labels, Subject labels, decision
rationale, and arbitrary nested objects.

Subject references remain local hypotheses owned by the sender. They are
shared only with the selected encrypted audience and must be rendered as
“the sender's Subject hypothesis,” never as a global identity.

## Receiving semantics

Only a trusted pinned Packet sender is projected into
`relationship-disclosures.json`. The persisted entry records the authenticated
Packet ID, sender, receive time, and validated disclosure.

The received-disclosure book is deliberately separate from
`relationships.json`. Receiving a disclosure:

- does not observe or link an Actor in the receiver's relationship book;
- does not create, merge, split, or supersede a Subject hypothesis;
- does not set a circle, label, contextual trust estimate, or suggestion;
- does not change PeerBook trust, capability, approval, or authorization;
- is idempotent on Packet ID and disclosure ID.

A later explicit local observation may cite a disclosure as evidence, but that
is a distinct action and is not part of v1.

## CLI

Send the first page:

```text
anet --home <A_HOME> relation-disclose <B_NODE_ID> --limit 100
```

Continue from the returned cursor or restrict the intentional disclosure:

```text
anet --home <A_HOME> relation-disclose <B_NODE_ID> \
  --after <RAC_CURSOR> --limit 100
anet --home <A_HOME> relation-disclose <B_NODE_ID> \
  --subject <SUBJECT_REF>
```

Read received observer views without importing them into local relations:

```text
anet --home <B_HOME> relation-disclosure-list
anet --home <B_HOME> relation-disclosure-list --sender <A_NODE_ID>
```

Derive a display-ready Reported relationship view for one sender:

```text
anet --home <B_HOME> relation-reported-view <A_NODE_ID>
anet --home <B_HOME> relation-reported-view <A_NODE_ID> \
  --subject <A_SUBJECT_REF> --include-activities
```

The derived view folds repeated structural reports into reported Subject
states, Actor links, circles, contextual trust estimates, transition lineage,
and interaction counts. Every value remains attributed to A. It reports
`completeness: partial-unknown` because v1 does not prove a historical baseline
or cross-Packet append continuity. Packet and disclosure provenance, receive
times, cursor heads, and coverage warnings remain visible.

Overlapping disclosed pages are deduplicated by activity ID. A conflicting body
for the same activity ID fails closed instead of choosing one. Arrival and
issue times are useful provenance but do not prove the sender's complete
cross-Packet event order.

`relation-disclose` requires an already pinned destination. Queueing is not
delivery; run the normal Anet service or synchronization path.

## Observer-local schedules

A node may maintain a revocable, expiring instruction to disclose future
activity to exactly one pinned audience:

```text
anet --home <A_HOME> relation-disclosure-schedule-add <B_NODE_ID> \
  --all --interval 300 --lifetime 2592000
anet --home <A_HOME> relation-disclosure-schedule-list
```

Use `--subject <SUBJECT_REF>` instead of `--all` to bind the instruction to one
local Subject hypothesis. New schedules start at the current global activity
cursor, so enabling one does not silently reveal existing history. Historical
replay requires the explicit `--include-history` flag.

The long-running `anet serve` loop prepares and queues due pages. An operator
or test may run due schedules once:

```text
anet --home <A_HOME> relation-disclosure-schedule-run
anet --home <A_HOME> relation-disclosure-schedule-run \
  --schedule <RDSC_ID>
```

Naming one schedule forces an immediate active-schedule check; it cannot bypass
revocation or expiry. Revoke locally with exact confirmation:

```text
anet --home <A_HOME> relation-disclosure-schedule-revoke <RDSC_ID> \
  --confirm <RDSC_ID> --reason observer-stopped
```

Schedule state is private in
`relationship-disclosure-schedules.json`. A prepared batch is persisted before
queueing, so a crash retries the same deterministic disclosure ID. The
receiver deduplicates that ID even if retries arrive in distinct Packets.
Revocation discards any pending batch before another send.

This is a local disclosure instruction, not a remote subscription. The audience
cannot create it, pull from it, widen its Subject scope, change its interval,
renew it, or prevent revocation. It authorizes only the named disclosure flow;
it never modifies PeerBook trust, capabilities, approvals, or contextual trust.

## MCP

The default MCP configuration sets:

```text
ANET_MCP_ALLOW_RELATION_DISCLOSURE=0
```

An operator may explicitly enable it for a scoped process. The tools are:

- `anet_relation_disclose` — queue one disclosure to an allowed peer;
- `anet_relation_disclosures` — read trusted received disclosures;
- `anet_relation_reported_view` — derive one sender-attributed partial view.

`ANET_MCP_ALLOWED_PEERS` still limits destinations. Enabling disclosure does
not enable raw Inbox access or relationship activity unless those separate
capabilities are also enabled.

## v1 omissions

v1 does not define audience-initiated subscriptions, remote deletion or
redaction after receipt, multi-audience broadcast, automatic evidence import,
or automatic renewal. These require separate consent and retention semantics.
