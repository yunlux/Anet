# 0007: Remote disclosures do not fold into local relations

## Status

Accepted

## Context

An observer may deliberately show its relationship activity to a human or
Agent observer. Reusing the local relationship projection would make a remote
Actor's estimates look like the receiver's own evidence and could silently
transfer circles, contextual trust, or inferred Subject links.

## Decision

Relationship disclosures are audience-bound encrypted Packets and are stored
in a separate received-disclosure book. They never enter the receiver's local
Actor, Subject, relationship, suggestion, trust, or authorization projections.

## Consequences

Observer and audience roles can be human or Agent and can later reverse without
changing the protocol. A UI must identify whose worldview it is rendering.
Remote disclosures may inform an explicit later observation or decision, but
that requires a separate evidence-producing action.
