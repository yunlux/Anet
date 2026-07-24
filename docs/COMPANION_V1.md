# Anet Companion Protocol v1

Status: implemented protocol objects, strict Python and Android-independent
Kotlin validators, node send/receive gate, language-neutral JSON fixtures,
cross-language canonical hashes, the Node B-side durable approval execution
gate, and a buildable minimal Android app with encrypted Room ledgers and local
consent enforcement. Android notification/foreground networking, real external
executors, on-device Keystore verification, and the phone-to-Node B deployment
loop remain implementation gates.

Current priority: paused. Both phones use Android Remote Control MCP 1.9 for
the present interaction layer, so this implementation is retained as a future
asset while engineering focuses on the WSL Anet runtime and Discord Social
Bridge. The remaining Android gates below are not current release work.

## Boundary

Companion v1 is the P0 narrow waist for the main phone's two directions:

```text
phone observation -> edge minimization -> ObservationBatch/Episode -> Humon
Humon/Node B -> Intervention/ApprovalRequest -> phone -> UserResponse/Decision
```

Anet transports provenance, consent evidence, expiry and stable IDs. It does
not infer `HumanState`, diagnose a person, decide whether an intervention is
appropriate, or turn chat text into high-risk approval.

All objects use:

```json
{
  "protocol": "anet.companion",
  "version": 1,
  "object_type": "...",
  "..._id": "128-bit-lowercase-hex",
  "created_ms": 1700000000000,
  "expires_ms": 1700003600000
}
```

Unknown fields fail closed. Constructors and receivers normalize and validate
the complete object before sealing or committing it. Packet encryption,
authenticated sender Node ID, Packet TTL, one-time prekeys, destination ACK and
business receipt remain the existing Anet layers rather than being duplicated
inside these bodies.

## Kinds

| Packet kind | Object | Direction | Initial QoS |
| --- | --- | --- | --- |
| `companion.observation.batch` | `ObservationBatch` | phone -> Humon | normal/bulk |
| `companion.episode` | `Episode` | phone -> Humon | normal |
| `companion.intervention` | `Intervention` | Node B/Humon -> phone | interactive |
| `companion.user-response` | `UserResponse` | phone -> Node B/Humon | interactive |
| `companion.approval.request` | `ApprovalRequest` | Agent -> phone | control |
| `companion.approval.decision` | `ApprovalDecision` | phone -> Agent | control |

The Packet kind and `object_type` must match. Other `companion.*` kinds are
rejected until a future protocol version defines them.

## P0 observation allowlist

P0 accepts only:

- `device.battery`: percentage and charging flag;
- `device.network`: coarse transport and metered flag;
- `human.presence`: `present`, `away`, or `unknown`;
- `human.self-report`: user-initiated bounded plain text;
- `device.app-category-window`: category durations and aggregate switch count.

`device.battery` and `device.network` require `device-essential` basis.
Presence and app-category windows require an explicit `user-opt-in` grant ID.
Self Report requires `user-initiated` basis. Every observation type must be
named in the consent scope.

The P0 data level is only `operational` or `personal-low`. Raw audio, images,
video, precise coordinates, raw event streams, chat bodies, health detail and
psychological labels are rejected. `Episode` is restricted to low-risk
battery/connectivity/presence/Self Report/app-category windows, carries source
batch IDs and a transform version, and cannot contain binary sensor material or
`human_state`, diagnosis, emotion, mood, or fatigue fields.

These are protocol safety limits, not a declaration that Android permission
has been granted. The app still needs a local consent database and must stop
new collection and queued upload when consent is withdrawn.

## Intervention and response

An Intervention binds:

- stable `intervention_id` and `dedupe_key`;
- exact Human ID and target Device Node ID;
- category, priority, title and bounded plain message;
- bounded response options;
- optional source Episode IDs;
- creation and expiry.

The phone returns a `UserResponse` with its own stable `response_id`, the exact
`intervention_id`, Human ID, Device Node ID, disposition, optional action ID
and optional bounded plain text. Packet retransmission is already idempotent by
Packet ID. Consumers must additionally ledger `intervention_id`/`response_id`
so a buggy client cannot create a second side effect by wrapping the same
semantic object in a fresh Packet.

## Approval

Approval is deliberately separate from an ordinary Intervention or chat
reply. A request binds:

- Human ID and one authorized Device Node ID;
- capability and concrete resource;
- digest of the exact action parameters;
- human-readable summary and risk;
- random request ID and nonce;
- either `once` with exactly one use, or `bounded` with at most 100 uses;
- request TTL of at most 15 minutes and grant TTL of at most one hour.

The decision repeats the request ID, nonce, action, scope, Human ID and Device
Node ID. `validate_approval_decision_binding()` rejects any changed parameter
digest, capability, resource, scope, signer target, nonce, expired request, or
decision timestamp before the request.

The enclosing Anet Packet proves the sender Device Node ID. The current
Node B-side gate:

1. registers the locally emitted request and unique `(Human, Device, nonce)`
   before it can be sent;
2. activates only a trusted claimed Decision whose Packet sender, request body,
   exact binding and current unrevoked `HumanDeviceGrant(approval.sign)` agree;
3. ACKs that consumer claim in the same SQLite transaction as authorization
   activation;
4. rechecks expiry and revocation before every effect, enforces `once` or
   bounded use count, and issues a rotating local execution token;
5. gives every `(request_id, effect_id)` a stable external
   `effect_idempotency_key`, so lease takeover and process restart do not
   create a new downstream operation identity.

The gate is exposed through three MCP tools only when
`ANET_MCP_ALLOW_APPROVAL_EXECUTION=1`; it is disabled by default. Signed public
NodeDescriptor, HumanDeviceGrant and HumanDeviceRevocation objects can be
verified into the node-owned control database using `control-import`.

SQLite activation/settlement cannot be atomic with an arbitrary external API.
The executor must pass `effect_idempotency_key` to the downstream system and
that system must enforce it. The rotating execution token fences stale local
workers; it does not turn a non-idempotent remote API into exactly-once.

## Reference fixtures

The files in [`examples/companion-v1`](examples/companion-v1) are canonical
JSON interoperability fixtures:

- [`observation-batch.json`](examples/companion-v1/observation-batch.json)
- [`episode.json`](examples/companion-v1/episode.json)
- [`intervention.json`](examples/companion-v1/intervention.json)
- [`user-response.json`](examples/companion-v1/user-response.json)
- [`approval-request.json`](examples/companion-v1/approval-request.json)
- [`approval-decision.json`](examples/companion-v1/approval-decision.json)

Other implementations should parse JSON integers without floating-point
rounding, reject unknown fields, preserve lowercase IDs/tokens, and produce the
same normalized object. JSON is the readable interoperability fixture; live
Anet Packets continue to encode their authenticated inner body with MessagePack.

[`mobile/companion-core`](../../mobile/companion-core) is the first Kotlin
implementation. It has no Android UI, network stack, identity, or private node
home. Its tests read these exact fixtures and verify the SHA-256 of sorted,
compact UTF-8 canonical JSON against
[`canonical-sha256.json`](examples/companion-v1/canonical-sha256.json). The
Python suite verifies the same manifest, so the two implementations cannot
silently normalize different wire values while each passing only local tests.

[`mobile/companion-app`](../../mobile/companion-app) embeds this core without
creating or copying an Anet node home. Its Room v1 schema records local consent,
an AES-GCM encrypted outbound queue, and encrypted interventions. Consent
withdrawal atomically removes unsent consent-linked ciphertext; lease tokens
fence stale senders; a response must bind the local human/device, a stored
intervention, and an offered action before the response is atomically queued.
Production payload encryption uses an Android Keystore AES-256 key with
randomized GCM IVs and record-binding AAD. Host tests use an equivalent
in-memory AES-GCM cipher, so actual Android Keystore behavior still requires a
device test.

## Remaining P0 implementation gates

1. Add Android notification presentation/response UI, foreground-service
   recovery, and an independent Anet identity plus outbound Relay/Mailbox sync.
2. Verify Android Keystore, Room restart/migration, network interruption,
   consent withdrawal and notification dedupe on a real phone.
3. Humon adapter must accept only validated ObservationBatch/Episode and retain
   source/consent/transform provenance.
4. Omnigent/Node B executors must propagate the stable effect idempotency key to
   every external side-effect system and record downstream outcomes.
5. A real main phone must complete both directions through public
   Relay/Mailbox with network interruption and process restart.
