# Ahub v1 Operations

This runbook operates the P0.2 Rendezvous/Mailbox service implemented by
`AhubService`. It does not operate an Anet node.

## State boundary

Choose a dedicated deployment-owned `AHUB_ROOT`. It is not `ANET_HOME` and
must not contain `identity.json`, `tls-key.pem`, `config.json`, or
`anet.sqlite3`. The CLI refuses a root containing those node-home markers.

The Ahub root contains:

- `ahub.sqlite3`: allowlist, durable request nonces, encrypted mailbox bytes,
  quota metadata, claim leases, signed settlement proofs, and Relay
  reservations;
- `control.sqlite3`: public signed descriptors and reachability checkpoints;
- SQLite WAL/SHM files while the service is running.

There are no node or human private keys. The directory is still sensitive
because it contains relationship, timing, address, packet-size, uploader, and
destination metadata. The implementation attempts `0700` on the root and
`0600` on database files where the platform supports POSIX modes.

Never initialize a node in this root, copy a node home into it, or point
`--home` at it.

## Install

Install the package with the minimal ASGI server extra:

```powershell
python -m pip install "anet-fabric[ahub]"
anet ahub-serve --help
```

The extra uses Uvicorn as a single-process ASGI server. Anet disables Uvicorn's
path-bearing access log, forwarded-header trust, and server banner. Application
logs contain only method, bounded route class, status, body byte count, and
elapsed time; they do not contain Node IDs, packet IDs, request bodies, claim
tokens, or network paths.

## Provision nodes

Obtain each complete Node ID from signed public material and verify it over an
authenticated second channel. Labels, IP addresses, partial IDs, email
accounts, and chat accounts are not authorization identities.

```powershell
$root = "D:\AnetAhub"
anet ahub-allow --root $root <COMPLETE_NODE_ID>
anet ahub-nodes --root $root
anet ahub-status --root $root
```

`ahub-nodes` is a local operator command and intentionally reveals the
allowlist. `ahub-status` reports only aggregate counts and byte/age metrics.

Disable a lost or unwanted node with exact confirmation:

```powershell
anet ahub-disallow --root $root <COMPLETE_NODE_ID> `
  --confirm <COMPLETE_NODE_ID>
```

Disablement immediately blocks authentication, publication, lookup, uploads,
and claims. Existing ciphertext addressed to or uploaded by the node remains
until normal expiry; the command does not silently destroy pending data.
Re-running `ahub-allow` is an explicit local re-enable operation.
An already paired Relay stream is not synchronously interrupted by the SQLite
change; it remains bounded by its negotiated byte/duration/expiry limits. Stop
the Ahub process as well when immediate live-stream termination is required.

Human-device revocation is a different control-plane action. Removing Ahub
access cannot substitute for revoking a `HumanDeviceGrant`.

## Bind and publish

The safe default binds only loopback:

```powershell
anet ahub-serve --root $root --host 127.0.0.1 --port 8422
```

Expose it through a TLS reverse proxy. The proxy must:

- require modern HTTPS and redirect or refuse plaintext Internet traffic;
- preserve `/v1/...` paths exactly because method and path are signed;
- preserve the four `X-Anet-*` authentication headers;
- support WebSocket upgrades on `/v1/relay/...` while preserving the exact
  signed path and authentication headers;
- disable WebSocket compression and apply idle/handshake admission limits;
- allow request bodies through `16 MiB + 1 byte` and responses through 24 MiB;
- use bounded connection/body timeouts and per-source admission limits;
- disable or redact path/access logs because paths contain Node or Packet IDs;
- not translate an HTTP account, cookie, or bearer token into Anet identity;
- not rewrite custody responses into delivery or task-completion events.

The CLI refuses a non-loopback bind unless `--allow-non-loopback` is explicit.
That flag does not add TLS. Use it only inside a protected container/private
network or when the selected reverse proxy cannot reach loopback.

Uvicorn proxy-header trust is disabled, so `X-Forwarded-For` is not an
authorization or audit identity. The current deployment is intentionally one
worker with SQLite WAL; horizontal multi-instance coordination is not yet a
supported topology.

## Health and local metrics

The unauthenticated loopback/proxy health endpoint reveals no queue counts:

```text
GET /healthz
{"protocol":1,"service":"anet-ahub","status":"ok"}
```

It runs SQLite `quick_check` on both databases. Use local aggregate metrics:

```powershell
anet ahub-status --root $root
```

The status includes enabled/disabled node counts, descriptor count, live and
expired reachability counts, mailbox packets/bytes, active claims, oldest
packet age, retained nonce/settlement counts, Relay reservation counts, and
database version. It does not list Node IDs or payload material.

Alert at minimum on:

- health not `ok`;
- mailbox bytes/oldest age growing continuously;
- expired packets remaining before scheduled cleanup;
- repeated HTTP 403/429 rates from the sanitized application logs;
- repeated Relay forbidden/busy/byte-limit/duration-limit categories;
- disk free space approaching the configured mailbox quota envelope.

## Cleanup

Normal requests opportunistically remove expired packets and nonces and release
expired claims. A timer may run:

```powershell
anet ahub-purge --root $root
```

This command only deletes expired packets/nonces and releases expired leases.
It does not remove live ciphertext, signed descriptor checkpoints, allowlist
records, or human/device records.

## Backup and restore

The two SQLite databases form one logical service state but are not a single
cross-database transaction. Use an offline backup:

1. stop the owning `ahub-serve` process;
2. run `anet ahub-checkpoint --root <ROOT>`;
3. confirm both `busy` values are zero;
4. copy `ahub.sqlite3` and `control.sqlite3` together to encrypted,
   access-controlled storage;
5. do not copy stale `-wal`/`-shm` files after a successful stopped checkpoint.

Restore both files together into an empty dedicated Ahub root, retain
restricted permissions, run `ahub-status`, then start the service and verify
`/healthz`. Do not restore these databases into a node home. Unlike a node-home
backup, an Ahub backup contains no node private identity, but its metadata and
retained ciphertext still require protection and a deletion policy.

## Minimal Linux service template

Review and adapt this template; the repository does not install or start it:

```ini
[Unit]
Description=Anet public Ahub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=anet-ahub
Group=anet-ahub
UMask=0077
ExecStart=/opt/anet/venv/bin/anet ahub-serve --root /var/lib/anet-ahub --host 127.0.0.1 --port 8422
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/anet-ahub

[Install]
WantedBy=multi-user.target
```

Keep the TLS reverse proxy and Ahub as different process identities where
practical. Back up and upgrade with the service stopped until a coordinated
online snapshot procedure exists.

## Current release boundary

This service provides Rendezvous, a bounded live byte Relay, and an adaptive
`AhubStoreCarrier` for asynchronous encrypted Mailbox custody. The carrier
persists its public
descriptor revision checkpoint in the owning node home, marks upload as
path-only custody, and accepts delivery only from the destination's signed
settlement proof. Verified proofs are explicitly acknowledged so the pending
proof queue remains bounded; their deduplication tombstones survive until
Packet expiry. It currently carries only depth-zero packets directly to their
final destination; arbitrary multi-hop forwarding is deliberately rejected.

Relay reservations survive restart, while live sessions intentionally do not.
The live Relay authenticates both Node IDs, allows one explicit peer, enforces
frame/direction/duration/node bounds and forwards binary bytes with
backpressure. Explicit node APIs now run the existing end-to-end TLS identity,
sync and receipt protocol through an unadvertised loopback bridge. When an
Ahub carrier is configured with `--live-relay` and explicit `--peer`, the
owner node continuously refreshes its bounded reservation/listener; an allowed
peer discovers only its matching reservation and AdaptiveRouter attempts the
live path before retaining Mailbox StoreCarrier behavior for offline work.
Ahub restart recovery is automatic, but this remains single-worker P0 code
without production external rate limiting or real public/mobile validation.

Example node-side configuration:

```powershell
anet --home <HOME> carrier-add https://ahub.example `
  --type ahub --name public --peer <COMPLETE_PEER_NODE_ID> `
  --live-relay --relay-reservation-ttl-seconds 900 `
  --relay-session-seconds 300 --relay-bytes-each-direction 67108864
```

Only one enabled live owner may be configured for the same Ahub URL and peer.
Every persistent runtime keeps its own private node home; do not copy identity,
TLS, config, Card, or SQLite files between machines to create the other side.

The service does not yet provide QUIC, NAT traversal, Companion, multi-Ahub
federation, public registration, multi-worker coordination, or a production
rate-limiter. See [`RELAY_V1.md`](RELAY_V1.md) for the live-stream boundary and
next adaptive integration gate.
