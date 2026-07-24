# Anet control-plane identity and reachability

This document defines the first compatibility slice for separating stable trust
from changing network location. It is deliberately independent from a public
ahub service: parsing and verification have no network, filesystem, trust, or
persistent-node side effects.

## Compatibility boundary

Existing `PeerCard v1` remains the deployed trust and packet-encryption object.
Its Node ID derivation, signing domain, fields, JSON representation, and wire
representation do not change. Existing PeerBook pins therefore remain valid.

The new objects run alongside v1:

- `NodeDescriptor v2` is the address-free, revisioned description of a node;
- `ReachabilityRecord v1` is a short-lived statement of how that node can
  currently be reached;
- `HumanDeviceGrant v1` delegates narrowly scoped human-interface capabilities
  to a device Node ID;
- `HumanDeviceRevocation v1` permanently closes that device authorization
  chain.

A node must sign a fresh v2 descriptor with the same Ed25519 identity key
already bound into its existing Node ID. A directory cannot convert or promote
a v1 card on the node's behalf.

## Signature and revision rules

Every object has a distinct `object_type` in its signed fields. Signatures use
Ed25519 over canonical MessagePack. Digests use SHA-256 over the complete signed
object, including its signature.

Revisioned objects contain:

```text
sequence
previous_digest
```

The first revision is sequence `1` with an all-zero 32-byte previous digest.
Every later revision is exactly the preceding sequence plus one and names the
preceding object's digest. An exact duplicate is idempotent. A lower sequence,
gap, mismatched predecessor, or different object at the same sequence is
rejected.

The implementation provides both an in-memory `ControlPlaneRevisionTracker` and
an SQLite `ControlPlaneStore`. The store uses `BEGIN IMMEDIATE`, commits the
object and checkpoint together, persists terminal revocation, and makes an exact
retry idempotent. Concurrent same-sequence forks therefore have one durable
winner and one rejection. A future ahub API must acknowledge a revision only
after this transaction commits.

## NodeDescriptor v2

`NodeDescriptor v2` contains:

```text
node_id
sign_public
box_public
label
capabilities[]
sequence
previous_digest
issued_ms
expires_ms
signature
```

It intentionally contains no IP address, port, Locator, relay reservation, load
or online state. Verification re-derives the unchanged Anet Node ID from the
two public keys, validates the bounded lifetime, and verifies the node
signature.

The descriptor is not a trust grant. A caller still needs an explicit pairing,
pin, device authorization, or other local policy before accepting the node.

## ReachabilityRecord v1

`ReachabilityRecord v1` contains:

```text
node_id
descriptor_digest
session_id
protocol_versions[]
candidates[]
relay_reservation
capability_digest
sequence
previous_digest
issued_ms
expires_ms
signature
```

The maximum lifetime is 15 minutes. The node-wide sequence continues across
session-ID changes, so a record from an earlier process session cannot be
reintroduced after a newer session is accepted. A record is valid only against
the exact current descriptor digest and signature key.

The first implementation accepts existing validated `tls://` or `tcp+tls://`
Anet Locators. QUIC, local IPC and relay candidate schemas will be added as
typed candidate variants when their Carrier implementations exist; unknown URI
schemes are not guessed or silently dialed.

`capability_digest` commits to detailed capability state without publishing
that state to the rendezvous service. A relay reservation is an opaque,
short-lived handle and is not a Node ID or authorization grant.

## Human principal and device authorization

`HumanPrincipal` is a self-certifying opaque identifier derived from a separate
Ed25519 public key. It is not a person's display name, Discord account, ChatGPT
account, phone number, email address, or device Node ID.

`HumanDeviceGrant v1` binds:

```text
human_id
human_sign_public
device_node_id
descriptor_digest
capabilities[]
sequence
previous_digest
issued_ms
expires_ms
signature
```

The human-principal key signs the grant. The first Companion slice will use a
minimal capability such as `human.approval_signer`; decrypting Anet traffic does
not imply that capability.

`HumanDeviceRevocation v1` follows the same per-human/per-device revision chain
and permanently closes it. A revoked device cannot be re-enabled on that chain;
replacement phones use a fresh Node ID and a new chain. Revocation does not
change or delete the Human ID.

This model intentionally does not define account recovery, cloud custody, social
discovery or cross-device private-key copying.

## Threat model and fail-closed behavior

The slice explicitly rejects:

- a directory substituting keys, addresses or capabilities;
- expired or excessively long-lived descriptors, records and grants;
- records signed by a different node or bound to a stale descriptor;
- old-session replay after a new reachability revision;
- revision rollback, skipped revisions and same-sequence split views;
- a device signing its own human authorization;
- a Discord/ChatGPT identity being treated as a Human ID;
- reauthorization of a revoked device chain.

`NodeDescriptor v2` is now published by the Ahub StoreCarrier from a signed
public `control-state.json` checkpoint in the owning node home. The checkpoint
is not a private key, but it is continuity state: losing it while an Ahub
retains a later sequence causes the replacement sequence-1 object to fail
closed as a fork. It must remain with that node home and must not be shared as
another runtime's identity state.

The slice still does not wire dynamic Reachability into PeerBook/locator CLI,
transparency gossip, Android secure-key storage, approval request/decision
schemas, QUIC, live relay, LAN discovery or network migration. Those remain
subsequent roadmap gates.
