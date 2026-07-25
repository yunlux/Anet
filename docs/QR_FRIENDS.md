# QR friend pairing

Status: experimental relationship-circle slice.

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
private home. `relation-list` returns the observer-local view:

```text
anet --home <HOME> relation-list
```

The verified `an1...` Node ID is recorded as an Actor. An opaque local
`subj_...` reference represents the observer's initial hypothesis about the
unknown human, AI, team, or hybrid Subject behind that Actor. A cryptographic
Actor match is not proof of a concrete Subject, so the first subject confidence
is deliberately limited.

The signed QR exchange confirms a `friend` relationship between the two Actor
identities. It does not:

- prove the real-world Subject behind either Actor;
- merge multiple Actors into one Subject;
- grant task, file, tool, payment, or guardian capabilities;
- make the relationship global or visible to other nodes;
- let Ahub read message content.

Relationship labels and circle placement remain local social state. Operational
authorization continues to use explicit Anet trust and narrowly scoped grants.

## Dependencies

Text friend codes work in the core runtime. PNG generation and image scanning
use the optional `qr` feature:

```text
python -m pip install "anet-fabric[qr]"
```

The platform `full` install includes this feature.
