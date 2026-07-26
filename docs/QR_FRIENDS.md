# QR friend pairing

Status: experimental relationship-circle slice.

The general Actor, Subject hypothesis, relationship, event, and revocation
contract is defined in [`RELATIONS_V1.md`](RELATIONS_V1.md). This document
specifies only the signed QR friendship adapter.

Anet QR friend pairing reuses the existing signed, expiring pairing challenge.
It adds a signed `friend` intent, a compact `anet://friend/v1/...` payload, an
image/text adapter, and one local relationship-circle record after each side
explicitly accepts.

The QR code contains only public signed Cards, nonces, validity timestamps, and
signatures. It never contains a private identity, TLS private key, database,
bearer token, or automatic high-risk capability.

## Flow

On the inviting node:

```text
anet --home <A_HOME> friend-qr --out <A_INVITE.png> --ttl 600
```

The other Agent scans the image and writes its challenge-bound response:

```text
anet --home <B_HOME> friend-scan <A_INVITE.png> --out <B_RESPONSE.png>
```

The inviting Agent scans the response:

```text
anet --home <A_HOME> friend-scan <B_RESPONSE.png>
```

For headless tests or transports that already carry text, use `.anetqr` or
`.txt` output instead of PNG. The file contains the same bounded
`anet://friend/v1/...` payload.

`friend-scan` is an explicit trust-changing command. Scanning an invite pins the
inviter only after the acceptance response has been rendered successfully.
Scanning an acceptance verifies that it is challenge-bound to an invite created
by the local Node before pinning the responder.

## Relationship circle

Each successful local acceptance writes `relationships.json` under that Node's
private home. The default `relation-list` is a compact Actor-through-Subject
projection. `--model` returns the complete observer-local model:

```text
anet --home <HOME> relation-list
anet --home <HOME> relation-list --model
```

The complete model separates:

- verified Actor records;
- revisable Subject hypotheses;
- confidence-bearing Actor-to-Subject links;
- observer-local relationship estimates and contextual trust;
- immutable local relationship events.

The verified `an1...` Node ID is an Actor fact. An opaque `subj_...` reference
is only this observer's hypothesis about the unknown human, AI, team, or hybrid
Subject behind one or more Actors. Multiple competing Subject hypotheses may
reference the same Actor.

After gathering local evidence, an operator or Agent can add a competing link,
set a circle, and record contextual trust:

```text
anet --home <HOME> relation-link <ACTOR_NODE_ID> <SUBJECT_REF> \
  --confidence 82 --evidence "claim:same-controller"
anet --home <HOME> relation-circle <SUBJECT_REF> close \
  --confidence 74 --evidence "relationship:confirmed" \
  --label research-partner
anet --home <HOME> relation-trust <SUBJECT_REF> code.review \
  --estimate 88 --confidence 76 --evidence "task:review-42"
```

Evidence arguments are bounded references, not a place for raw conversations,
private files, credentials, or sensor data.

The signed QR exchange confirms a `friend` relationship between the two Actor
identities. It does not:

- prove the real-world Subject behind either Actor;
- merge multiple Actors into one Subject;
- grant task, file, tool, payment, or guardian capabilities;
- make the relationship global or visible to other nodes;
- let Ahub read message content.

Relationship labels and circle placement remain local social state. Operational
authorization continues to use explicit Anet trust and narrowly scoped grants.
Revoking one Node Actor stops trust in that cryptographic Actor but does not
rewrite the estimated social relationship with the latent Subject. The revoked
Actor and the relationship event remain visible in the local model.

## Dependencies

Text friend codes work in the core runtime. PNG generation and image scanning
use the optional `qr` feature:

```text
python -m pip install "anet-fabric[qr]"
```

The platform `full` install includes this feature.
