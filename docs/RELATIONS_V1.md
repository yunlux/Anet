# Anet observer-local relations v1

Status: experimental P0 domain contract.

Anet Relations lets one observing node maintain a local social model from
verified Actor facts, revisable Subject hypotheses, evidence-backed
relationship estimates, and immutable local events. It is not a global
identity registry, reputation authority, authorization system, or claim that
the concrete human, AI, team, or hybrid entity behind an Actor is known.

## Domain separation

### Actor

An Actor is a currently verifiable or explicitly attributed source of action.
Anet Nodes retain their complete `an1...` Node ID as the Actor ID. Platform
Adapters derive opaque, observer-safe `act_<platform>_<digest>` Actor IDs from
an Adapter-owned pseudonym and the attesting Node namespace. Raw account IDs,
usernames, channel IDs and session tokens are not Actor IDs.

An Actor fact can establish:

- which Node key signed an object;
- which Node ID participated in a challenge-bound pairing;
- which local Adapter observed an opaque platform account pseudonym;
- which signed bridge Node attested to a platform observation;
- which key was pinned or revoked locally;
- which evidence reference caused a model update.

It cannot establish whether the controller is a human, AI, team, hybrid,
shared account, or delegated runtime.

Each Actor carries one or more typed proofs:

- `cryptographic`: locally verified Node key/card evidence;
- `platform-observed`: a local Adapter directly observed the account source;
- `bridge-attested`: a signed Anet peer reported its Adapter observation;
- `operator-attested`: an explicit local operator assertion.

The scopes are descriptive, not a numeric strength ladder. A
`bridge-attested` Discord Actor is distinct from the bridge Node Actor. It does
not inherit that Node's circle, contextual trust, PeerBook trust, capabilities
or authorization. The attesting Node namespace is included in the derived
Actor ID, so two unrelated bridges cannot accidentally collapse their local
pseudonyms into one Actor.

### Subject hypothesis

A Subject hypothesis is one observer's revisable estimate of a latent entity
behind one or more Actors. Its `subj_...` reference is opaque and local to one
relationship book. It is never exchanged as a universal identity.

One Actor can appear in multiple competing Subject hypotheses. One Subject
hypothesis can link several Actors. Each Actor-to-Subject link carries its own
confidence, evidence references, and update time.

New evidence may:

- strengthen or weaken one link;
- add a competing hypothesis;
- make another hypothesis the primary local projection;
- later justify explicit split, merge, or supersession operations.

The original Actor observations and events remain unchanged.

### Subject transition

A Subject transition records how the observer revised its explanation:

- `supersede`: one hypothesis is replaced by one revised hypothesis;
- `merge`: several hypotheses are replaced by one combined hypothesis;
- `split`: one hypothesis is replaced by an exact, non-overlapping partition
  of its Actors.

Every replacement receives a new opaque `subj_...` reference. Sources remain
in the book with state `superseded`, their relationships become dormant, and
the transition records both sides of the lineage, confidence, evidence
reference, and time. A transition never claims that a real entity was
literally divided or combined.

One-to-one supersession inherits the source relationship. Merge and split
default every replacement to `known` with no contextual trust. The caller may
explicitly choose exactly one source-to-replacement inheritance path; the same
relationship or trust estimate is never multiplied across replacements.

### Relationship estimate

A Relationship estimate describes the observer's current relationship with
one Subject hypothesis. It contains:

- one social-distance circle;
- local relationship labels;
- confidence in that relationship estimate;
- zero or more contextual trust estimates;
- evidence references and update time.

It is not a signed statement by the other party unless a referenced protocol
object separately proves that statement.

### Relationship event

A Relationship event is an immutable local record explaining why the model
changed. Events identify their type, relevant Actor and Subject references,
one bounded evidence reference, and observation time.

Evidence references must not contain raw conversations, credentials, private
files, human sensor data, or other sensitive material. They should point to a
locally controlled evidence object or stable event identifier.

### Interaction evidence

Interaction evidence is a content-free observation that a verified Actor
participated in an application interaction. It contains:

- a deterministic local evidence ID;
- the linked Actor and observer-local Subject reference;
- incoming or outgoing direction;
- one or more coarse facets: `message`, `task`, `skill`, or `artifact`;
- a coarse context and outcome;
- the Packet reference and occurrence time.

The projector may inspect a validated in-memory payload only to recognize
coarse facets and task outcome. It never copies message text, task objectives,
task output, filenames, file contents, credentials, or private context into
`relationships.json`.

Network probes, receipts, prekey traffic, and Companion control messages are
not social interactions. Projection runs after durable queueing or acceptance
and fails independently, so a damaged relationship book cannot reject or
undo a valid Packet.

## Relationship circles

The ordered default circles are:

```text
public → known → collab → friend → close → family
```

They describe social distance only.

- `public`: no established local relationship;
- `known`: recognized and under observation;
- `collab`: repeated cooperation in a bounded context;
- `friend`: explicitly confirmed social relationship;
- `close`: sustained high-context relationship;
- `family`: the nearest long-term relationship circle.

Labels describe relationship meaning within a circle. Examples include
`research-partner`, `mutual-guardian`, `partner`, and `mentor`. A label does not
create a capability.

Circle changes are explicit local model updates. Interaction counts, recency,
task evidence, or model suggestions may recommend a change, but they must not
silently declare friendship, intimacy, family, guardianship, or delegation.

## Contextual trust

Trust is recorded per narrow context:

```text
message
code.review
artifact.render
file.open
payment.approve
```

Each context has an estimate and a confidence value. A high estimate in one
context does not transfer to another context. Missing evidence means
uncertainty, not automatic distrust.

Context trust supports explanation and policy suggestions. It is not an Anet
capability and cannot bypass PeerBook trust, MCP scopes, task capabilities,
human-device grants, or application-specific approval.

## Persistent model

`relationships.json` version 7 is private node-local state with eight persisted
sections:

```text
observer Actor
typed Actors and scoped proof records
Subject hypotheses and Actor links
Relationship estimates
Relationship events
Content-free interaction evidence
Subject transition lineage
Immutable relationship-suggestion decisions
```

`relation-list --model` adds derived `interaction_stats`,
`relationship_suggestions`, and `relationship_activity` projections; none is
persisted.

The loader accepts version 1 through version 6 books. It projects v1
one-Actor/one-Subject records into the current model, treats a v2 book as
having no interaction evidence, treats a v3 book as having no Subject
transitions, and assigns legacy Node Actors a migrated cryptographic Peer Card
proof. Version 5 has no suggestion-decision history. The next mutation writes
version 7. Version 6 events have no structured historical details; they remain
valid and project with an empty detail object. Migration does not increase
Subject confidence or invent interaction evidence, transitions, decisions,
events, or event details.

Interaction statistics are derived from evidence and are deliberately not
trust scores. Repeated traffic can increase a count but cannot raise
contextual trust or grant a capability.

One runtime owns one relationship book. Books must not be copied between node
homes to make two observers appear to share a worldview.

## Observer-local activity feed

The activity feed is a derived, content-free chronological view over the
relationship book's immutable event spine. Read the first page and continue
with its opaque cursor:

```text
anet --home <HOME> relation-activity --limit 100
anet --home <HOME> relation-activity --after <RAC_CURSOR> --limit 100
anet --home <HOME> relation-activity --after <RAC_CURSOR> --wait 30
anet --home <HOME> relation-activity --subject <SUBJECT_REF>
```

`--wait` performs one bounded long poll and returns as soon as a matching
activity appears or the timeout expires. It does not start another node,
listener, Ahub, or background process.

The cursor is bound to the observer Actor and exact event position. A cursor
from another node home fails instead of reading a different worldview. Pages
preserve durable append order rather than sorting by occurrence time: a delayed
Discord or task observation can carry an older `occurred_ms` while still
appearing after the cursor position at which it was persisted.

Each activity identifies its fact level (`verified`, `inference`, `estimate`,
or `decision`) and contains only bounded structural details. Evidence
references and decision rationale are replaced by digests; message text, task
objectives/results, filenames, credentials, Subject labels, and Actor labels
are not projected. Every page and item reports `authorization_effect: none`.
The feed is a replay/read model, not a shared timeline or authority ledger.

For repeated Agent polling, the optional MCP tool
`anet_relation_activity` exposes the same projection only when
`ANET_MCP_ALLOW_RELATION_ACTIVITY=1`. It is disabled by default.

## Audience-bound remote observation

An observer may explicitly send one selected activity page to a pinned peer:

```text
anet --home <A_HOME> relation-disclose <B_NODE_ID> --limit 100
anet --home <A_HOME> relation-disclose <B_NODE_ID> \
  --after <RAC_CURSOR>
anet --home <A_HOME> relation-disclose <B_NODE_ID> \
  --subject <SUBJECT_REF>
```

The resulting `social.relationship.disclosure` Packet is authenticated,
destination-bound and end-to-end encrypted. Its body repeats the observer and
audience Actor IDs and accepts only the content-free activity allowlist.

The receiver reads the sender's worldview separately:

```text
anet --home <B_HOME> relation-disclosure-list --sender <A_NODE_ID>
anet --home <B_HOME> relation-reported-view <A_NODE_ID>
```

Received disclosures are stored in `relationship-disclosures.json`, not folded
into `relationships.json`. They cannot create a local Actor, Subject, circle,
contextual trust estimate, PeerBook trust, capability, or authorization.
`relation-reported-view` is a derived, sender-attributed read model for UI and
Agent consumption. It always declares unknown coverage because v1 cannot prove
a complete baseline or continuous order across Packets. Scheduled v2
disclosures add a fixed series, sequence, baseline, scope, and cursor links, so
one selected segment can be verified as continuous or fail visibly with a gap.
Continuity still does not establish current state or external truth.
See [`RELATIONSHIP_DISCLOSURES_V1.md`](RELATIONSHIP_DISCLOSURES_V1.md).

## CLI interface

Compact projection:

```text
anet --home <HOME> relation-list
```

Complete model:

```text
anet --home <HOME> relation-list --model
```

The complete model also contains a compact `mutual_relationship_claims`
projection for claims stored in this node home. It has participant Actor IDs,
their current local Subject references when known, circle, labels, active or
withdrawn state, and bounded withdrawal metadata. It never inserts a claim
into a Subject or changes a local relationship estimate. The `/social` page
renders this projection only in the browser after a local file import.

The `/social` demonstration can render an exported model locally in the
browser:

```text
anet --home <HOME> relation-list --model > relation-model.json
```

Choose **导入本地模型** on the demo page. The file is parsed in the browser and
is not uploaded by the static demo.

Add a competing Actor-to-Subject link (Node or typed opaque Actor ID):

```text
anet --home <HOME> relation-link <ACTOR_ID> <SUBJECT_REF> \
  --confidence 82 --evidence "claim:same-controller"
```

Observe a non-Node source under an explicit local operator assertion. This is
the bootstrap route for a person or external Agent that does not have an Anet
Node. The Actor ID must already be an opaque `act_<namespace>_<32-hex>` value;
never place a raw account ID, username, email, phone number, or session token
in it or in the display label:

```text
anet --home <HOME> relation-observe-actor act_local_<32-hex> \
  --kind human.local --confidence 35 \
  --evidence "operator:relationship-bootstrap"
```

The command always records `operator-attested`, creates only a new local
Subject hypothesis at `public`, and reports `identity_assertion: none` and
`authorization_effect: none`. It cannot observe an `an1...` Node; use its
signed Peer Card for that. Repeating the identical observation is idempotent.

Withdraw one local external Actor observation with an exact confirmation:

```text
anet --home <HOME> relation-actor-revoke act_local_<32-hex> \
  --confirm act_local_<32-hex> --reason "operator:source-retired"
```

This marks only the Actor as revoked and appends one local `actor.revoked`
event. It retains the Subject hypothesis, circle, contextual trust, and any
other linked Actors for explicit later revision. For a Node Actor use
`peer-revoke`, which additionally cleans its cryptographic transport state.

Set a circle and optional labels:

```text
anet --home <HOME> relation-circle <SUBJECT_REF> close \
  --confidence 74 --evidence "relationship:confirmed" \
  --label research-partner
```

Set contextual trust:

```text
anet --home <HOME> relation-trust <SUBJECT_REF> code.review \
  --estimate 88 --confidence 76 --evidence "task:review-42"
```

End one observer-local relationship estimate with an exact confirmation:

```text
anet --home <HOME> relation-end <SUBJECT_REF> --confirm <SUBJECT_REF> \
  --reason "operator:relationship-ended"
```

This records `relationship.ended` in local history. It does not revoke linked
Actors, delete the Subject hypothesis, erase contextual trust, alter mutual
relationship claims, modify PeerBook, or change authorization. Repeating the
same command is idempotent. A later explicit `relation-circle` action reopens
the local relationship as `active`; setting contextual trust alone does not.
The local activity feed projects the change as the content-free
`relationship.ended` estimate, so a local `/social` model import can replay it
without exposing the reason or evidence reference.

For a relationship that is merely inactive rather than concluded, use the same
observer-local confirmation pattern with `relation-pause`:

```text
anet --home <HOME> relation-pause <SUBJECT_REF> --confirm <SUBJECT_REF> \
  --reason "operator:relationship-inactive"
```

This records `relationship.paused` and sets the relationship state to
`dormant`; it is excluded from active circle counts and from relationship
suggestions. It retains the same local Subject, Actor, evidence, contextual
trust and claim boundaries. An explicit `relation-circle` reactivates it.

Revise one hypothesis one-to-one while inheriting its relationship:

```text
anet --home <HOME> subject-supersede <SUBJECT_REF> \
  --confidence 84 --evidence "claim:revised-explanation"
```

Merge hypotheses. Without `--inherit`, the replacement starts as `known`:

```text
anet --home <HOME> subject-merge <SUBJECT_A> <SUBJECT_B> \
  --confidence 78 --evidence "claim:same-controller" \
  --inherit <SUBJECT_A>
```

Split one hypothesis into an exact Actor partition. Only the selected
one-based group inherits the previous relationship:

```text
anet --home <HOME> subject-split <SUBJECT_REF> \
  --group <ACTOR_A>,<ACTOR_B> --group <ACTOR_C> \
  --confidence 83 --evidence "claim:controllers-diverged" \
  --inherit-group 1
```

These commands mutate observer-local social state. They do not mutate PeerBook
trust or create task, tool, file, payment, guardian, or delegation capability.

## Explainable relationship suggestions

`relation-suggest` evaluates the current private model without mutating it:

```text
anet --home <HOME> relation-suggest
anet --home <HOME> relation-suggest --subject <SUBJECT_REF>
```

Every deterministic suggestion contains a basis hash, confidence, structured
metrics, rationale codes, evidence tags, explicit accept/reject decision
commands, and the proposed underlying mutation for inspection. The Agent may
inspect that output and make its own decision; merely generating a suggestion
changes nothing.

The default advisor has only two narrow policies:

- a `known` Subject may be reviewed for `collab` after repeated task submission
  and completion evidence exists in both directions;
- at least three incoming completed/failed task results may produce a
  Bayesian-shrunk review candidate for `task.delivery` contextual trust.

It never suggests `friend`, `close`, `family`, Subject linking, Actor identity,
PeerBook trust, capabilities, or authorization. Message traffic, Discord
reputation, reactions, account age and social labels cannot satisfy either
policy. A relationship suggestion is not persisted and cannot apply itself.
`relation-decide` is the auditable mutation seam. The lower-level
`relation-circle` and `relation-trust` commands remain available for direct
operator-authored estimates that did not originate from an advisor suggestion.

## Suggestion decisions

An observer can accept or reject only a suggestion that the advisor can still
reproduce from the current evidence:

```text
anet --home <HOME> relation-decide <SUGGESTION_ID> accepted \
  --reason "agent:bounded-collaboration-confirmed"
anet --home <HOME> relation-decide <SUGGESTION_ID> rejected \
  --reason "agent:insufficient-social-context"
anet --home <HOME> relation-decision-list
anet --home <HOME> relation-decision-list --subject <SUBJECT_REF>
```

Evidence changes produce a new deterministic suggestion ID. An undecided old
ID is then stale and cannot be accepted or rejected. One suggestion can have
only one immutable decision; an idempotent retry of the same decision returns
the existing record, while attempting to reverse it fails.

Acceptance atomically applies exactly the proposed `known -> collab` circle or
`task.delivery` contextual-trust value, records its evidence basis and
rationale, and appends decision and relationship events in one save. Rejection
records the same audit basis without changing the relationship. Neither path
changes PeerBook trust, Subject links, capabilities, or authorization.

`relation-list --model` includes persisted `suggestion_decisions` and only
currently active, undecided `relationship_suggestions`. Decision rationale is
a bounded content-free reference, not a place for conversation text or private
evidence.

## Discord Actor projection

The Discord Adapter persists source events first, then idempotently projects
content-free metadata into the same relationship book:

```text
Discord HMAC actor pseudonym
  -> namespace-bound act_discord_<digest>
  -> account.discord Actor + scoped proof
  -> fresh local Subject hypothesis
  -> content-free social.discord Interaction evidence
```

A generic channel observation starts in `public`. A direct mention or reply
may recognize the Subject as `known` with low confidence. Scores, reactions,
operator labels, and the bridge Node's relationship never create contextual
trust or a closer circle.

Normal `anet serve` operation projects newly persisted events automatically.
After repairing or upgrading a relationship book, a stopped runtime can replay
the durable ledger safely:

```text
anet --home <HOME> discord-social-project --limit 1000
```

Replay uses stable event references and is idempotent. Output contains derived
Actor IDs and counts, not raw Discord account, Guild, channel, message content,
or token data.

## Revocation semantics

Peer revocation applies to one verified Actor key. It prevents that Actor from
continuing to use local Anet trust and records an `actor.revoked` relationship
event.

It does not prove that:

- the latent Subject disappeared;
- all Actors controlled by that Subject are compromised;
- the social relationship ended;
- another Actor should inherit the revoked Actor's authority.

Consequently, Actor revocation does not erase or automatically downgrade the
Subject-level circle. The UI must show the revoked Actor and current reachability
separately from the social relationship.

## Ahub and exchange boundary

Ahub remains an untrusted-to-read rendezvous, mailbox, and relay. It does not
calculate Subject hypotheses, circles, labels, or trust.

Two Actors may exchange a proposal and counter-sign it as a mutual relationship
claim. The portable object contains Actor cards, a circle, public labels, and
both signatures but no Subject references. Each participant projects it into
its own local model, never into a shared identity or global relationship
registry. See [`RELATIONSHIP_CLAIMS_V1.md`](RELATIONSHIP_CLAIMS_V1.md).

Ahub may carry an encrypted claim as ordinary end-to-end payload, but it does
not inspect, calculate, or project the relationship.

## Current omissions

Relations v1 does not yet standardize:

- mutual claim replacement or jointly acknowledged ending;
- custom advisor policies and decision supersession;
- standing subscriptions, withdrawal, or remote deletion for relationship
  disclosures;
- multi-audience or offline-portable relationship disclosure bundles;
- Web3 attestations or public reputation.

These omissions preserve the Actor/Subject and inference/authorization
separations while the local model and UI are validated.
