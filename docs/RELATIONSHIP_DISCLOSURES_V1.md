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

Scheduled disclosure uses the compatible v2 body type
`anet.relationship.disclosure.v2`. It adds digest-bound:

- an opaque `rdsr_` series ID and zero-based sequence;
- `starts_after` and `next_cursor` continuity links;
- fixed `all` or one-Subject scope;
- a `history-start` or `current-cursor` declared baseline.

Sequence zero establishes the declared baseline. Later items prove continuity
only when every sequence is present and each `starts_after` equals the previous
`next_cursor`. Transport receive order and wall-clock issue time never replace
those links.

A Subject-scoped v2 series may send an encrypted zero-activity checkpoint when
the observer cursor advances only across other Subjects. The checkpoint reveals
no out-of-scope activity; it prevents a silent cursor jump from looking
continuous.

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

For v2, select one series when several exist:

```text
anet --home <B_HOME> relation-reported-view <A_NODE_ID> \
  --series <RDSR_ID>
```

The result is `proven-continuous-segment` only when sequence and cursor links
verify. Missing sequence or cursor mismatch produces `gap-detected`.
`history-through-cursor` means the declared history-start scope is continuous
through that cursor; `declared-baseline-through-cursor` covers only a
current-cursor baseline. Neither status proves current state after the last
cursor or that the observer perceived all external reality.

When `gap-detected` includes `missing_sequences`, the audience may report its
observation without gaining pull authority:

```text
anet --home <B_HOME> relation-disclosure-gap-notice <A_NODE_ID> \
  --series <RDSR_ID>
```

The encrypted `social.relationship.disclosure.gap-notice` body contains only
the audience Actor, observer Actor, series, missing sequence numbers, detection
horizon, and explicit `requested_action: none`, `scope_change: false`, and
`authorization_effect: none` boundaries. It reports B's local delivery view;
it does not prove A failed to send or that a carrier lost a Packet.

After receiving a trusted notice, A may inspect it and independently choose to
retransmit:

```text
anet --home <A_HOME> relation-disclosure-gap-notice-list \
  --reporter <B_NODE_ID>
anet --home <A_HOME> relation-disclosure-gap-retransmit <RGAP_ID>
```

Retransmission uses the exact digest-identical page retained in A's private
`relationship-disclosure-archive.json`. It is permitted only when the original
schedule is still active, goes only to its original audience, does not change
scope, and does not advance the series. Revoked or expired schedules fail
closed. A page outside the bounded archive is reported as unavailable rather
than reconstructed from newer state.

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

New schedules persist a series ID and next sequence. Existing v1 schedule files
migrate to schedule v2 with a fresh deterministic series at the already stored
cursor and `current-cursor` baseline. A pending legacy v1 batch may finish
idempotently; the subsequent v2 series starts from the resulting cursor without
claiming that pre-migration delivery was continuous.

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
- `anet_relation_reported_view` — derive one sender-attributed partial view;
- `anet_relation_gap_notice` — report visible missing sequences;
- `anet_relation_gap_notices` — read trusted advisory notices;
- `anet_relation_gap_retransmit` — resend exact archived pages under an active
  original schedule.

`ANET_MCP_ALLOWED_PEERS` still limits destinations. Enabling disclosure does
not enable raw Inbox access or relationship activity unless those separate
capabilities are also enabled.

## v1 omissions

v1 does not define audience-initiated subscriptions, disclosure pulls, remote
deletion or redaction after receipt, multi-audience broadcast, automatic
evidence import, or automatic renewal. Gap notices deliberately remain
advisory and do not add any of those powers.
