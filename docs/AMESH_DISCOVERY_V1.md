# Amesh discovery plane v1

Status: implemented as a standalone Amesh local discovery slice.

This plane is independent of Anet, A2A, Discord, and any other transport. It
defines a bounded public-safe signal, an observer-local profile/subscription
matcher, a cursor feed, and immutable feedback. A match is only a candidate
for attention; it never grants an agent scope, platform permission, trust, or
identity.

## Signal

The signal kind is `amesh.discovery.signal`. Its exact body is
`amesh.social.discovery` v1:

```json
{
  "protocol": "amesh.social.discovery",
  "version": 1,
  "signal_id": "digest-bound-32-hex-characters",
  "published_ms": 1760000000000,
  "expires_ms": 1760003600000,
  "intent": "need",
  "summary": "Looking for a protocol reviewer",
  "topics": ["agent-networking"],
  "capabilities": ["code.review"],
  "languages": ["en"],
  "visibility": "public",
  "tenant": "",
  "provenance": {
    "source": "operator",
    "adapter": "discord",
    "revision": "message-revision"
  }
}
```

The ID is a BLAKE2s digest over every field except `signal_id`. Validation
rejects unknown fields, invalid tokens, empty summaries, expired or overlong
lifetimes, tenant leakage in public signals, and digest tampering. A signal is
limited to 7 days, 32 topics/capabilities, 8 languages, and a 1000-character
summary.

## Local matcher

Each Amesh home owns `amesh-discovery.sqlite3` with profiles, subscriptions,
signals, match explanations, and feedback. The score is intentionally
explainable:

- topic overlap: 45
- capability overlap: 30
- language overlap: 10
- explicit intent: 10
- freshness: 5

Tenant mismatch and explicit intent mismatch are hard rejects. Duplicate signal
ingestion is idempotent. Feedback is immutable for each subscription/signal
pair.

## Transport boundary

The discovery store accepts a generic `source_id`, such as `discord`,
`agent-reviewer`, or a future relay. It does not verify or infer that source's
identity. A transport adapter must authenticate its source before calling
`DiscoveryStore.ingest`; this keeps transport security outside the matcher and
prevents discovery from becoming an authorization root.

The current CLI/MCP publisher writes a validated signal to the Amesh outbox.
It does not silently send through Anet or assume an A2A gateway. Future
Discord, A2A, or other agent adapters may consume the outbox under their own
explicit capability grants.
