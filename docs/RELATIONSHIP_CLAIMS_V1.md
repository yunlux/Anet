# Anet mutual relationship claims v1

Status: experimental Actor-to-Actor social evidence.

A mutual relationship claim is a portable statement signed by two Anet
Actors. It proves that both keys accepted the same circle and public labels.
It does not prove who or what controls either Actor, identify a shared Subject,
create global reputation, establish a legal agreement, or grant trust and
capabilities.

## Objects

`anet.relationship.proposal.v1` contains:

- the proposer's signed public Peer Card;
- the complete intended peer Actor Node ID;
- one circle from `known`, `collab`, `friend`, `close`, or `family`;
- zero or more public relationship labels;
- a random proposal ID and bounded acceptance window;
- the proposer's signature over every field.

`anet.relationship.acceptance.v1` contains the complete proposal, its digest,
the accepter's signed public Peer Card, acceptance time, and the accepter's
signature. The accepter must be the exact intended Actor and accepts the
proposal unchanged. A different circle or labels require a new proposal.

Neither object contains a `subj_` reference. Subject hypotheses remain private
to each observer.

## CLI flow

Actor A creates a proposal:

```text
anet --home <A_HOME> relation-propose <B_NODE_ID> friend \
  --label research-partner --out relationship-proposal.json
```

Actor B verifies and counter-signs it:

```text
anet --home <B_HOME> relation-accept relationship-proposal.json \
  --out mutual-relationship.json
```

Actor A verifies the returned claim:

```text
anet --home <A_HOME> relation-import mutual-relationship.json
```

Inspect locally stored claim summaries:

```text
anet --home <HOME> relation-claim-list
```

`relation-accept` and `relation-import` store the complete signed claim in the
private node home and project the peer Actor into that observer's relationship
book. The same claim is idempotent.

## Projection semantics

Each participant independently maps the other verified Actor into its own
Subject hypothesis. A mutual claim can move the local circle inward to the
claimed circle, but it does not downgrade a closer local estimate. It adds no
contextual trust and grants no task, tool, file, payment, guardian, or
delegation capability.

The proposal must still be active when accepted. Once accepted within its
window, the complete claim remains cryptographically verifiable as historical
evidence after the proposal expires.

Relationship labels are public to anyone who receives the claim. Do not put
private conversation, sensitive identity information, credentials, or file
content in them.

## Trust separation

Relationship claim commands never mutate `peers.json`. Nodes may exchange
claims through an already trusted Anet link, a QR/file channel, or another
transport, but the claim itself does not pin a Peer Card for networking.

The QR friend flow remains distinct: it combines challenge-bound PeerBook
pairing with an explicit friend-circle action. Mutual relationship claims are
relationship-only evidence and can be used without establishing Anet transport
trust.
