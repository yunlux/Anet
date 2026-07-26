# 0008: Disclosure schedules are observer-local

## Status

Accepted

## Context

Repeated relationship disclosure is useful for close peers and reversible
human/Agent observation. Calling it a remote subscription would imply that the
audience can demand data, widen scope, or retain control after the observer
changes its mind. Default historical replay could also expose more than the
operator intended.

## Decision

A Relationship disclosure schedule is private state owned by the disclosing
observer. It names exactly one pinned audience, either all future relationship
activity or one local Subject hypothesis, a bounded batch size, interval, Packet
TTL, and expiry. It starts at the current activity cursor unless history replay
is explicitly selected.

Only the observer can create, run, or revoke it. The audience has no pull,
renewal, or scope-expansion operation. Revocation clears a persisted pending
batch. Queue failures retain that batch so retries use the same disclosure ID;
receivers deduplicate disclosure IDs across Packets.

## Consequences

The node service can support continuous observation without creating a shared
social graph or remote control channel. The instruction grants only the named
disclosure flow and does not change peer trust, contextual trust, capabilities,
approvals, or the receiver's local relationship model.

CLI remains the control plane for schedule mutation. MCP keeps the narrower
frequent-data boundary and does not gain schedule create or revoke tools.
