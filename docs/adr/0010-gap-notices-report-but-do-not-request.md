# 0010: Gap notices report but do not request

## Status

Accepted

## Context

A disclosure audience can prove that sequence numbers are absent from its local
copy, but allowing it to request a page or history would invert the
observer-owned disclosure boundary. Recreating a missing page from current
relationship state could also rewrite history under an old sequence number.

## Decision

The audience may send an authenticated, digest-bound gap notice containing only
the series, missing sequence numbers, and observed horizon. The notice declares
`requested_action: none`, cannot change scope, and grants no authority. The
observer may independently retransmit the exact archived disclosure body only
to its original audience and only while the original schedule remains active.
Retransmission does not advance the series. Revoked or expired schedules fail
closed, and an unavailable archived page remains visibly unavailable.

## Consequences

Gap detection becomes actionable without creating audience pull semantics.
Scheduled pages require a bounded private sender archive, protected as node
state. A notice can be repeated or ignored; it is evidence of the receiver's
local delivery view, not proof that the observer failed to send or that a
carrier lost the Packet.
