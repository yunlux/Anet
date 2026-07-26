# 0009: Disclosure series prove segments, not reality

## Status

Accepted

## Context

Relationship disclosure v1 authenticates each Packet and preserves append order
inside one page, but it does not identify the page's starting cursor or its
relationship to another Packet. A receiver cannot distinguish continuous
delivery from a missing page. Ordering by receive time or sender wall-clock time
would hide transport reordering and clock uncertainty.

Subject-scoped schedules create another edge case: the observer may advance
across events for other Subjects without disclosing those events. Silently
moving the cursor would look like a gap even when no authorized Subject event
was omitted.

## Decision

Scheduled disclosures use v2 series metadata: an opaque series ID, monotonic
sequence, starting cursor, fixed scope, and a declared `history-start` or
`current-cursor` baseline. Each accepted item is digest-bound and
audience-bound. The receiver proves a continuous segment only when sequence
numbers begin at zero without gaps and every next `starts_after` equals the
previous `next_cursor`.

A v2 series may contain a zero-activity checkpoint. It advances the cursor
without exposing events outside a Subject-scoped schedule. A checkpoint is
still authenticated, encrypted, sequence-bound, and digest-bound.

One-shot v1 disclosures remain readable. Existing v1 schedules migrate to a new
deterministic series at their current cursor with a `current-cursor` baseline;
Anet does not invent continuity for disclosures sent before migration.

## Consequences

Transport arrival order no longer controls replay order for one selected
series. Missing or conflicting sequence/cursor links fail visibly as
`gap-detected`. A continuous history-start series proves coverage only through
its last accepted cursor. A current-cursor series proves continuity only from
its declared baseline. Neither proves that the observer perceived all reality
or that no newer event exists.
