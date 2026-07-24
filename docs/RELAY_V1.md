# Anet Ahub Relay v1

Status: implemented P0.2 live-byte, node TLS/sync, persistent owner lifecycle,
peer-scoped discovery, and automatic live-first/Mailbox-fallback integration.

## Purpose and boundary

Ahub Relay v1 pairs two explicitly authorized Anet Node IDs over two outbound
WebSocket connections and forwards opaque binary bytes with bounded resources.
It gives mobile, NATed, and firewalled nodes a live path without requiring
public inbound ports.

The Relay is not a Packet mailbox, task broker, TLS endpoint, destination ACK,
or proof that an Agent received anything. `AhubHTTPClient.open_relay()`
exposes the raw bounded stream. `AnetNode.sync_ahub_relay_once()` carries the
existing client-side TLS 1.3, certificate channel binding, signed identity
handshake, Packet sync and receipt protocol over it.

The reservation owner uses `AnetNode.serve_ahub_relay_once()`. That method
creates an ephemeral, unadvertised loopback TLS listener owned by the node and
bridges its outbound Relay WebSocket to that listener. Both persistent node
configs may keep `listen_enabled=false`; no public inbound socket is required.
The Ahub forwards only the resulting TLS ciphertext.

With `live_relay_enabled`, `AnetNode.start()` maintains one bounded owner
reservation/listener per explicitly configured peer. The listener refreshes
after disconnect or Ahub restart with bounded jittered backoff. Before an
adaptive Ahub sync, the allowed peer discovers only the reservation whose
owner and `allowed_peer_id` match the signed caller, attempts the existing
node-to-node TLS sync, and then runs the StoreCarrier path for queued or offline
work. These are currently two measured paths inside one configured Ahub
carrier, not yet the P1 `SessionCarrier`/`StoreCarrier` interface split.

## Reservation

The owner sends an authenticated request:

```text
POST /v1/relay/reservations
{
  "allowed_peer_id": "an1...",
  "ttl_ms": 900000,
  "max_duration_ms": 300000,
  "max_bytes_each_direction": 67108864
}
```

The request uses the existing `AhubRequest v1` signature and durable nonce.
Both Node IDs must be locally allowlisted and have current descriptors. A
reservation:

- belongs to one owner and exactly one allowed peer;
- has a random 128-bit opaque ID;
- is persistent across Ahub restarts;
- is refreshed idempotently under the same owner/peer pair and ID;
- expires even if no peer connects;
- is limited per owner.

The reservation ID is a locator token, not an identity credential or bearer
authorization. Possessing it is insufficient: every WebSocket upgrade is
signed by the connecting Node ID.

The allowed peer discovers its matching reservation with:

```text
GET /v1/relay/reservations/{owner_node_id}
```

The signed caller is fixed as `allowed_peer_id`; the response never enumerates
other owners, peers, or reservations. Absence is returned as not found.

## Pairing and forwarding

Both endpoints connect outward to:

```text
GET /v1/relay/{reservation_id}
Upgrade: websocket
X-Anet-Node: ...
X-Anet-Issued: ...
X-Anet-Nonce: ...
X-Anet-Signature: ...
```

The exact GET path and empty body digest are signed. The nonce is transactionally
consumed before the upgrade is accepted. The owner opens first and receives
`relay.waiting`; the allowed peer then joins. Each endpoint receives one
`relay.ready` control containing the authenticated peer Node ID and negotiated
limits. After readiness, only binary frames are permitted.

The server awaits every destination ASGI send before reading more from that
direction, providing bounded backpressure rather than an unbounded application
queue. It counts bytes independently in both directions and closes both ends
when any of these conditions occurs:

- reservation expiry;
- negotiated session duration;
- per-direction byte allowance;
- maximum binary frame size;
- either endpoint disconnecting;
- text/control data after readiness;
- duplicate or over-capacity use.

The default limits are:

- reservation TTL: 15 minutes;
- live session duration: 5 minutes;
- 64 MiB in each direction;
- 64 KiB per WebSocket frame;
- four reservations and four active connections per node.

Operators may lower limits through `AhubLimits`. Clients cannot request a
value above the server maximum.

## Persistence and failure semantics

Reservation rows and authentication nonces are durable in
`ahub.sqlite3`. Live pairings are deliberately in memory:

- an Ahub restart terminates current streams;
- unexpired reservations remain usable after restart;
- endpoints reconnect with fresh signed requests and fresh nonces;
- bytes already forwarded are not replayed or acknowledged by the Relay.

Only one Uvicorn worker is supported. Multi-worker or multi-instance
coordination would require a shared live-session coordinator and is not
implemented.

Disabling a node blocks new signed requests and new Relay joins. An already
paired stream is bounded by its remaining duration/bytes but is not currently
interrupted by a database disable event. Human-device revocation remains a
separate control-plane action.

## Security and visible metadata

The Relay sees both Node IDs, reservation relationship, connection IP/timing,
frame sizes, direction byte totals, and disconnect reason. It must see enough
of this metadata to enforce abuse limits. It receives no node private key and
must never terminate the future Anet end-to-end TLS session.

Application logs use only the stable `relay_reservation` / `relay_stream`
route classes, result category, aggregate direction byte counts, and elapsed
time. They omit Node IDs, reservation IDs, paths, frame bodies, and message
content.

The production reverse proxy must support WebSocket upgrades without rewriting
the signed path or stripping the four `X-Anet-*` headers. Compression is
disabled in the reference client and Uvicorn server.

## Relationship to libp2p

The resource model follows the useful parts of
[libp2p Circuit Relay v2](https://github.com/libp2p/specs/blob/master/relay/circuit-v2.md):
explicit reservation, separate listener/connector roles, an already-open
connection to the target, and duration/byte limits that reset both sides.

Anet does not copy libp2p peer identity, multiaddress, voucher, stream
multiplexer, or DCUtR wire formats. Relay v1 also does not permit a Relay
connection itself to create nested reservations. Future direct-path upgrade
will authenticate the direct Anet session independently before
make-before-break migration.

## Node TLS integration evidence

The node bridge deliberately reuses the existing TLS/sync implementation rather
than creating Relay-specific trust semantics:

- TLS terminates exclusively at the two disposable nodes;
- the Ahub never sees Packet plaintext;
- both nodes can keep persistent inbound listening and direct dialing disabled;
- the authenticated sync records `ahub-relay:*`, not `direct`, as its path;
- Packet destination ACK, business receipt and both pending queues converge in
  one session;
- repeating the session produces one Inbox item;
- cancellation closes WebSocket, socket bridge and TLS tasks without a named
  Relay task leak.

## Next integration gate

The automatic path is implemented but is not yet a production
`SessionCarrier`. The next gates are:

- prove live-unavailable to Mailbox fallback without confusing custody,
  destination ACK, business receipt, or consumer ACK;
- package and operate the single-worker service behind production TLS and
  external rate limiting on a real public host;
- exercise client restart and mobile-network interruption, not only Ahub
  restart;
- in P1, split measured live sessions from asynchronous stores and authenticate
  a direct candidate independently before make-before-break Relay-to-direct
  migration without changing logical task/session identity.
