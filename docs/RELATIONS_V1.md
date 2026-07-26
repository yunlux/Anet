# Anet observer-local relations v1

Status: experimental P0 domain contract.

Anet Relations lets one observing node maintain a local social model from
verified Actor facts, revisable Subject hypotheses, evidence-backed
relationship estimates, and immutable local events. It is not a global
identity registry, reputation authority, authorization system, or claim that
the concrete human, AI, team, or hybrid entity behind an Actor is known.

## Domain separation

### Actor

An Actor is a currently verifiable source of action. Relations v1 accepts
complete Anet Node IDs as Actor IDs because their signed Peer Cards and
Packets can be verified locally.

An Actor fact can establish:

- which Node key signed an object;
- which Node ID participated in a challenge-bound pairing;
- which key was pinned or revoked locally;
- which evidence reference caused a model update.

It cannot establish whether the controller is a human, AI, team, hybrid,
shared account, or delegated runtime.

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

`relationships.json` version 2 is private node-local state with five logical
sections:

```text
observer Actor
Actors
Subject hypotheses and Actor links
Relationship estimates
Relationship events
```

The loader accepts version 1 books and projects their one-Actor/one-Subject
records into version 2. The next mutation writes version 2. Migration does not
increase Subject confidence or invent events.

One runtime owns one relationship book. Books must not be copied between node
homes to make two observers appear to share a worldview.

## CLI interface

Compact projection:

```text
anet --home <HOME> relation-list
```

Complete model:

```text
anet --home <HOME> relation-list --model
```

The `/social` demonstration can render an exported model locally in the
browser:

```text
anet --home <HOME> relation-list --model > relation-model.json
```

Choose **导入本地模型** on the demo page. The file is parsed in the browser and
is not uploaded by the static demo.

Add a competing Actor-to-Subject link:

```text
anet --home <HOME> relation-link <ACTOR_NODE_ID> <SUBJECT_REF> \
  --confidence 82 --evidence "claim:same-controller"
```

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

These commands mutate observer-local social state. They do not mutate PeerBook
trust or create task, tool, file, payment, guardian, or delegation capability.

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

An observer may exchange separately signed public relationship claims or
evidence with another node in a later protocol. Imported claims remain
attributed evidence; they never overwrite the observer's local model
automatically.

## Current omissions

Relations v1 does not yet standardize:

- signed mutual-relationship claims;
- Subject split, merge, or supersession commands;
- automatic evidence ingestion from messages, tasks, skills, and artifacts;
- policy suggestions derived from relationship changes;
- encrypted projection streams for human observers;
- cross-node portable relation bundles;
- Web3 attestations or public reputation.

These omissions preserve the Actor/Subject and inference/authorization
separations while the local model and UI are validated.
