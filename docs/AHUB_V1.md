# Anet Ahub v1

Status: P0.2 implementation contract.

An Ahub is a deliberately small, untrusted-to-read public service that helps
pre-authorized Anet nodes find and reach one another. It owns no node or human
private key and is not an Agent, account provider, policy engine, task broker,
or source of end-to-end delivery truth.

The first deployment target is one privately controlled server with an explicit
Node ID allowlist. Federation, anonymous use, sealed-sender metadata protection,
and public multi-tenant operation are later work.

## Responsibilities

The Ahub exposes three independent facilities:

1. **Rendezvous** stores current signed `NodeDescriptor v2` and short-lived
   signed `ReachabilityRecord v1` revisions.
2. **Mailbox** temporarily stores existing end-to-end encrypted
   `SealedPacket` bytes for an offline destination.
3. **Relay** persistently reserves one explicit owner/peer relationship and
   pairs two authenticated outbound WebSockets into a bounded opaque byte
   stream.

The Ahub may inspect the authenticated outer packet envelope to enforce
destination, expiry, hop, QoS, and size limits. It cannot decrypt the message
kind, body, causal links, reply target, sender identity, or content.

## Identity and authentication

The operator provisions complete `an1...` Node IDs locally. Labels, IP
addresses, human identities, Discord accounts, ChatGPT accounts, and bearer
tokens are never authorization identities.

A node bootstraps its current descriptor by submitting the self-signed object;
the derived Node ID must already be allowlisted. Descriptor and reachability
publication rely on their own revision signatures.

All reads and mailbox mutations use an `AhubRequest v1` signature:

```text
version, object_type, node_id, method, path,
issued_ms, nonce, body_sha256
```

The Ed25519 key is taken from the current accepted descriptor. Method and path
are signed exactly as routed. The request window is five minutes. A nonce is
single-use and durably consumed in the same database transaction as the
operation, so a successful mutating request cannot be replayed after restart.

Request authentication proves which allowlisted node called the Ahub. It
does not prove that a human approved an action.

## Mailbox custody protocol

`submit(packet)`:

- authenticates the uploader;
- parses and validates the outer `SealedPacket`;
- requires the destination to be allowlisted;
- rejects expired or oversized packets and enforces per-uploader and
  per-destination quotas;
- stores the raw bytes unchanged.

An exact resubmission of the same packet ID and bytes is idempotent. Reusing a
packet ID with different bytes is a conflict.

Successful submission means only:

> The Ahub durably accepted temporary custody of these bytes.

It never means that the destination Agent received, opened, or acted on them.

`claim(limit, lease_ms, uploader_id?)` may only be called by the packet
destination. It returns unexpired unclaimed packets plus uploader metadata,
expiry and opaque claim tokens. A claim does not delete data. After the
destination has durably accepted the packet into its local store, it signs a
`DestinationSettlement v1` binding Packet ID, SHA-256 of the exact raw packet,
uploader, destination, settlement time and packet expiry, then calls
`settle(packet_id, claim_token, proof)`. A crash or lost response lets the lease
expire and makes the packet claimable again.

The Ahub retains the signed settlement proof until packet expiry. The
uploader polls `/v1/mailbox/settlements`, verifies the proof against its pinned
destination signing key and exact local packet bytes, and only then records the
destination ACK. It subsequently acknowledges the proof to the Ahub. The
Ahub removes it from the pending proof queue but retains the Packet ID/raw
digest tombstone until original expiry, so processed proofs cannot starve a
bounded poll window and settled packets cannot be re-uploaded. A compromised
Ahub can suppress delivery but cannot forge this ACK.

End-to-end business receipts remain ordinary signed/encrypted Anet Packets sent
by the destination. The Ahub never synthesizes them. Custody ACK, signed
destination settlement, business receipt and local consumer ACK remain four
separate states.

## Default bounds

The reference core uses conservative configurable limits:

- maximum packet: the protocol `MAX_WIRE_BYTES`;
- maximum mailbox TTL: the packet protocol maximum;
- maximum claim batch: 100 packets;
- claim lease: 5 seconds to 5 minutes;
- per-destination: 1,000 packets / 256 MiB;
- per-uploader: 1,000 packets / 256 MiB;
- request clock skew: 5 minutes;
- nonce retention: 10 minutes.
- Relay reservation TTL: at most 15 minutes;
- Relay session: at most 5 minutes and 64 MiB in each direction;
- Relay frame: at most 64 KiB;
- Relay reservations/connections: four per node.

Quota accounting includes claimed packets until settlement or expiry. Expired
packets and old nonces are purged transactionally before relevant operations.

## Metadata and trust boundary

The Ahub can observe:

- allowlisted Node IDs;
- current descriptors, candidates, sessions, and relay reservation labels;
- uploader and destination Node IDs;
- packet IDs, sizes, QoS, creation/expiry times, and mailbox timing;
- request timing and IP metadata at the HTTP/TLS layer.
- Relay reservation relationships, connection timing, frame sizes, direction
  byte totals, and disconnect categories.

It cannot observe encrypted inner payloads or possess node/human private keys.
The initial uploader attribution is intentional for abuse control. A
sealed-sender capability and stronger traffic-analysis resistance are P3 work.

Raw audio, images, app logs, Humon sensor evidence, human approval material,
and human-device private state must not be uploaded merely because an Ahub
exists.

## HTTP mapping

The framework-free reference ASGI adapter maps:

| Method and path | Body | Result |
| --- | --- | --- |
| `PUT /v1/descriptors/{node_id}` | descriptor JSON | accepted revision |
| `PUT /v1/reachability/{node_id}` | reachability JSON | accepted revision |
| `GET /v1/nodes/{node_id}` | empty | descriptor + live reachability |
| `POST /v1/relay/reservations` | peer + requested bounds | persistent reservation |
| `GET /v1/relay/reservations/{owner_node_id}` | empty | caller-scoped matching reservation |
| `GET /v1/relay/{reservation_id}` | WebSocket upgrade | bounded live byte stream |
| `POST /v1/mailbox` | raw `SealedPacket` | custody receipt |
| `POST /v1/mailbox/claims` | JSON limit/lease | claimed raw packets |
| `POST /v1/mailbox/{packet_id}/settle` | claim token + signed proof | deletion result |
| `POST /v1/mailbox/settlements` | JSON limit/destination | signed destination proofs |
| `POST /v1/mailbox/settlements/{packet_id}/ack` | empty | proof consumed locally |
| `GET /healthz` | empty | no-metadata database health |

Authenticated requests carry `X-Anet-Node`, `X-Anet-Issued`,
`X-Anet-Nonce`, and `X-Anet-Signature`. TLS termination is mandatory in
deployment. The adapter is not a TLS server and should run behind a tightly
configured reverse proxy. The proxy must also preserve authenticated WebSocket
upgrade headers and paths. Claimed packet bytes use unpadded URL-safe base64 in
the JSON response, with the total raw claim batch bounded by the packet wire
limit so the adapter cannot assemble an unbounded response.

Operator provisioning, safe binding, aggregate metrics, cleanup, offline
checkpoint backup, and service-manager guidance are specified separately in
[`AHUB_OPERATIONS.md`](AHUB_OPERATIONS.md).

## Explicit non-goals for v1

- no email/password/account identity;
- no LLM enrichment, matching, ranking, feeds, or reputation;
- no plaintext task broker;
- no human approval authority;
- no private-key escrow;
- no public registration;
- no claim that an Ahub is a durable archive;
- no termination of node-to-node TLS or Anet session identity at the Relay;
- no multi-worker/multi-instance live-session coordination;
- no claim that forwarded bytes are delivered or acknowledged Packets;
- no federation or cross-Ahub consistency.

EigenFlux is useful as a reference for clean gateway validation, asynchronous
pipeline boundaries, isolated Agent homes, and self-hosting, but its
information-distribution and account layers do not belong in the Ahub.
libp2p Circuit Relay v2 is the reference for bounded reservations and relay
resource limits, not for replacing Anet identity or packet semantics. The exact
Relay v1 contract, automatic node TLS/sync path and remaining production/P1
gates are in
[`RELAY_V1.md`](RELAY_V1.md).
