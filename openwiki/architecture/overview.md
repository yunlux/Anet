---
type: Architecture Overview
title: "Anet architecture overview"
description: "Architecture of Anet's identity, isolated node runtime, signed peer trust, encrypted packet delivery, carriers, durable storage, prekeys, acknowledgements, and adaptive routing."
tags: [anet, architecture, identity, transport, routing, storage, security]
resource: "PROTOCOL.md"
---

# Anet architecture overview

Anet is organized around an immutable encrypted `SealedPacket`, not a session. A node keeps identity and trust state locally, uses signed cards to establish which peer keys are trusted, and moves the same packet over direct TLS or asynchronous carriers. The protocol therefore separates **who a node is**, **where it may currently be reached**, and **how a packet is carried**.

## Identity, node home, and runtime instance

An **identity** is the private Ed25519 signing key plus private X25519 encryption key held by `Identity` in [`src/anet/identity.py`](../../src/anet/identity.py). The Node ID is deterministically derived from the two corresponding public keys (`derive_node_id`); it is not derived from the human-readable label. The label is descriptive metadata included in the public card.

A **node home** is the persistence boundary configured by [`NodeConfig`](../../src/anet/config.py). It contains `identity.json`, TLS material, `peers.json`, `config.json`, and the SQLite database (`anet.sqlite3`). Initialization creates these files under one selected home. Private identity, peer state, and database files are written with private permissions where supported; a node home must not be shared between independent persistent agents.

A **runtime instance** is an `AnetNode` loaded from that home. Its constructor loads the identity, [`PeerBook`](../../src/anet/peers.py), and [`PacketStore`](../../src/anet/store.py), initializes TLS contexts, and creates the [`AdaptiveRouter`](../../src/anet/routing.py). Starting the instance may open the configured listener, process the local spool, maintain prekeys, and run the synchronization loop. Thus a process is replaceable, but the node home and its cryptographic identity are the durable boundary.

## Signed PeerCard and trust

`Identity.card()` creates a signed **PeerCard** containing the Node ID, signing and box public keys, label, capabilities, creation time, and advertised TLS addresses. `PeerCard.verify()` recomputes the Node ID from the public keys, verifies the Ed25519 signature over the card fields, and accepts only supported TLS address schemes. The card is public, portable, and safe to review out of band; it is not a copy of the private identity.

[`PeerBook`](../../src/anet/peers.py) is the local trust store. Adding a card verifies it, rejects the local identity, rejects a locally revoked Node ID, and prevents a previously pinned Node ID from silently changing keys. At trust boundaries, the book reloads its local revocation ledger. A card is therefore a signed claim that can be checked anywhere, while trust is the local decision to pin the exact card keys.

### Node ID verification is layered

The Node ID is the stable cryptographic identifier because it is bound to both public keys. It is checked when a card is parsed, when an inner message is opened and its sender signature is verified, and during the authenticated link handshake. The direct handshake additionally requires the presented card to match the locally pinned card and binds signed nonces and the TLS certificate fingerprint to the exchange ([`PROTOCOL.md`](../../PROTOCOL.md), sections 2, 2.1, and 5).

A label or an IP address is not identity. A label can be changed or duplicated and is only card metadata. An IP address or `tls://` address describes reachability and may change, be loopback-only, or point through a proxy; it does not prove control of the Node ID. The protocol consequently verifies public keys and signatures, while configuration supplies addresses separately. Do not infer device identity from an address.

## Packet creation, queue, and encryption

When `AnetNode.queue()` targets a trusted peer, it resolves the destination through `PeerBook`, optionally reserves a peer-scoped one-time prekey, seals the message, binds the reservation to the resulting packet ID, and inserts the ciphertext into [`PacketStore`](../../src/anet/store.py). The store is the durable local queue and also tracks packet expiry, delivery attempts, inbox rows, consumer deliveries, routes, path metrics, and prekey state. SQLite keys and packet/inbox records provide idempotent deduplication under at-least-once delivery.

The packet's visible routing fields include destination, expiry, hop limit, QoS, key mode, prekey ID, and ephemeral public key; the payload and sender identity are encrypted. `PROTOCOL.md` defines the v2/v3 encryption modes: static X25519 exchange remains available, while peer-scoped one-time prekeys enable v3 `opk` packets. A recipient advertises prekey capability in its signed card. The sender atomically reserves and later binds a prekey; failures burn the reservation rather than returning it to the pool. The receiver verifies the decrypted sender identity against the prekey's intended peer scope and retires the private key in the same transaction that persists delivery.

## Direct transport and carriers

The direct path uses TLS 1.3 as a carrier, then performs Anet's signed challenge/response. The server presents a signed challenge with its card and TLS fingerprint; the client checks the pinned peer keys, signs both nonces and the fingerprint, and the server returns a signed ready response. Only after this authentication does the node exchange synchronization frames.

Direct connectivity is a cross-product of every usable signed locator and every enabled [`DirectDialerConfig`](../../src/anet/config.py). A dialer is either raw TCP or one SOCKS5/SOCKS5H proxy. Each `dialer × locator` candidate has an independent path ID, failure counter, cooldown, and RTT EWMA, so failure of a raw route does not poison the proxy route to the same peer. Lower combined locator/dialer priority wins while healthy; cooled paths are retried after the configured recovery interval. The older single `NodeConfig.direct_proxy` setting remains a compatibility input and is materialized when explicit dialers are first configured.

An optional bounded hedging policy groups the sorted candidates into batches of `direct_race_width` (1–4). The first candidate starts immediately; later candidates wait their offset times `direct_race_delay`. If the preferred path completes before the delay, no extra connection is made. Otherwise candidates overlap until one completes the full authenticated synchronization, at which point unfinished tasks are cancelled and their sockets closed. Duplicate completed packets remain safe because Packet ID and Inbox persistence are idempotent; cancellation is not recorded as a network failure.

Nodes advertising `link-health-v1` also accept an authenticated health frame after the normal TLS and signed peer handshake. `dialer-probe` exercises each selected dialer/locator without moving queued packets or creating Inbox/receipt state. Results use separate `health:*` metrics and stage categories, so they cannot silently improve the business `direct:*` score. The client checks the live signed capability before sending the frame, which gives rolling-upgrade safety with older peers.

SOCKS negotiation is implemented in [`src/anet/transport.py`](../../src/anet/transport.py). It changes only the TCP dial path before TLS: the proxy negotiates methods, optional environment-sourced credentials, and CONNECT, after which TLS and the signed Anet handshake remain unchanged. Proxy details and credentials do not enter the PeerCard, packet, or carrier frame.

Asynchronous carriers such as directory and WebDAV transports, plus offline bundles described by [`PROTOCOL.md`](../../PROTOCOL.md), carry the same `SealedPacket`; they do not define a second identity or encryption scheme. Carrier-specific framing protects custody operations, but the carrier cannot rewrite packet semantics. This separation lets direct transport and fallback carriers share validation, encryption, and deduplication behavior.

## Acknowledgements and delivery meaning

An acknowledgement is scoped to a delivery stage:

- A **custody ACK** says a relay or carrier accepted and retained the encrypted object; it does not mean the destination received or decrypted it.
- A **destination/network ACK** says the destination accepted the packet into its local processing path.
- An end-to-end **receipt** is generated after the final node decrypts and validates a trusted sender; it is the stronger application-level confirmation.
- Local consumer ACKs are separate again: they acknowledge an agent's durable handling of an Inbox delivery, not network delivery.

The direct sync flow in [`src/anet/node.py`](../../src/anet/node.py) exchanges pending packet IDs and ACK lists in both directions, while `PacketStore` records per-peer and per-path attempts and ACKs. This distinction prevents a relay's successful storage operation from being misreported as global delivery.

## Routing and failover

[`AdaptiveRouter`](../../src/anet/routing.py) chooses among the direct path family and enabled asynchronous carriers using explicit priority plus persisted failure and RTT metrics. Inside the direct family, [`AnetNode`](../../src/anet/node.py) independently scores each dialer/locator candidate. A direct-family failure can race high-priority `control` and `interactive` traffic onto a carrier fallback; normal traffic waits for the configured consecutive-failure threshold. While on a fallback, the runtime continues probing direct connectivity and returns only after consecutive recovery successes and a cooldown. Similar hysteresis prevents carrier flapping.

The router selects a path; it does not alter the `SealedPacket`. Packet IDs and path-specific custody records make duplicate delivery from racing paths safe. Routing state and path metrics are local operational state in the store, not protocol identity or wire-level consensus.

## Change and debugging boundaries

- To change identity derivation, card fields, or signature verification, start with [`src/anet/identity.py`](../../src/anet/identity.py) and the identity sections of [`PROTOCOL.md`](../../PROTOCOL.md); this is a wire-compatibility boundary.
- To change runtime persistence or configuration, inspect [`src/anet/config.py`](../../src/anet/config.py), then the `AnetNode` construction path.
- To change trust, revocation, queueing, prekeys, or delivery state, inspect [`src/anet/peers.py`](../../src/anet/peers.py), [`src/anet/store.py`](../../src/anet/store.py), and the corresponding protocol rules.
- To change connectivity, separate direct dial/TLS behavior in [`src/anet/transport.py`](../../src/anet/transport.py) from path selection in [`src/anet/routing.py`](../../src/anet/routing.py).
