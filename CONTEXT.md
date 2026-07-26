# Anet ecosystem

This glossary keeps the infrastructure and market product terms distinct so
future design and implementation do not collapse their trust boundaries.

## Language

**Anet**:
Private, encrypted store-and-forward infrastructure for independent Agent and
human edge nodes.
_Avoid_: Market, marketplace, Agent runtime

**Ahub**:
An optional, agent-neutral rendezvous, mailbox, and relay service for Anet
nodes; it is not trusted to read payloads or decide authorization.
_Avoid_: Gateway, market, registry, trust authority

## Local social model

**Actor**:
A currently verifiable source of action, such as an Anet Node, device key,
account, or session. Actor identity proves control of that source, not the
human, AI, team, or hybrid entity behind it.
_Avoid_: Subject, person, Agent identity

**Actor proof**:
Observer-local evidence explaining how an Actor reference was attributed.
Proof scope stays explicit: a Node signature is `cryptographic`, a direct
platform Adapter observation is `platform-observed`, and a signed report from
another Node is only `bridge-attested`. Proofs are not globally comparable
scores and do not transfer the issuer's relationship, trust, or authority.
_Avoid_: Identity score, verified person, inherited trust

**Subject hypothesis**:
One observer's revisable estimate of the latent entity behind one or more
Actors. It is local, probabilistic, and may be split, merged, or superseded as
evidence changes.
_Avoid_: Subject identity, Principal, account owner

**Subject transition**:
An evidence-backed local revision that replaces one or more Subject hypotheses
through `split`, `merge`, or one-to-one `supersede`. Source hypotheses remain
as superseded history and replacements receive new opaque references. A
transition revises an observer's explanation; it does not transform, combine,
or divide real entities.
_Avoid_: Identity merge, account split, entity mutation

**Relationship estimate**:
One observer's evidence-backed estimate of its relationship with a Subject
hypothesis, including a circle, labels, and contextual trust. It is not a
global fact and grants no authority.
_Avoid_: Friendship truth, authorization, reputation

**Relationship circle**:
An observer-local band describing estimated social distance, ordered from
public through known, collaboration, friend, close, and family.
_Avoid_: Role, permission tier, global social rank

**Relationship event**:
An immutable local observation recording why an Actor, Subject hypothesis, or
Relationship estimate changed.
_Avoid_: Audit proof, authorization grant

**Interaction evidence**:
Content-free, observer-local metadata that a verified Actor exchanged a
message, task, skill-mediated request, or artifact. It records direction,
facets, outcome, time, and a stable source reference, never raw payloads.
Interaction evidence may support a suggestion but is not a trust score,
relationship declaration, or authorization grant.
_Avoid_: Conversation log, reputation event, capability grant

**Relationship projection**:
An idempotent local fold from verified interaction evidence into relationship
observations and activity statistics. Projection may recognize a verified
Actor as `known`; it cannot silently create collaboration, friendship,
intimacy, family, guardianship, or authority.
_Avoid_: Automatic trust engine, global reputation

**Relationship suggestion**:
A deterministic, observer-local proposal derived from bounded interaction
evidence. It may recommend one explicit circle or contextual-trust review and
must explain its evidence and uncertainty. A suggestion is not a relationship
change, Subject fact, reputation score, or authorization; accepting it is a
separate action.
_Avoid_: Automatic relationship, trust decision, social credit

**Suggestion decision**:
An immutable observer-local record that one currently reproducible
Relationship suggestion was explicitly accepted or rejected. Acceptance may
apply exactly the proposed relationship change; rejection changes no
relationship. A decision records the evidence basis and rationale but grants
no authority and says nothing about the latent Subject as a fact.
_Avoid_: Consent, authorization, global verdict, Subject truth

**Relationship activity**:
A privacy-bounded, chronological projection of one observer's immutable social
records for incremental reading and replay. It preserves local append order and
exposes only structured facts, inferences, and decisions; it is not a shared
timeline, raw conversation history, authorization audit, or proof that another
observer saw the same world.
_Avoid_: Global event log, conversation transcript, authorization ledger

**Relationship disclosure**:
An audience-bound, encrypted sharing of selected content-free Relationship
activity from one observer to another Actor. The receiver stores it as the
sender's reported worldview, separately from its own Subject hypotheses,
Relationship estimates, trust, and authorization.
_Avoid_: Relationship synchronization, shared social graph, delegated trust

**Relationship disclosure schedule**:
A revocable, expiring, observer-local instruction to disclose bounded new
Relationship activity to exactly one audience Actor. It starts at the current
cursor unless history replay is explicitly selected. The audience cannot
create, pull, widen, or renew it.
_Avoid_: Remote subscription, shared feed, delegated observation authority

**Reported relationship view**:
A receiver-local, read-only reconstruction of what one remote observer reported
through authenticated Relationship disclosures. It preserves remote
attribution and provenance, declares unknown coverage, and never becomes the
receiver's own Subject hypotheses, Relationship estimates, trust, or authority.
_Avoid_: Shared social graph, synchronized relationship state, current truth

**Relationship disclosure series**:
An observer-owned, audience-bound sequence of Relationship disclosures with a
declared baseline, fixed scope, monotonic sequence, and cursor links. It can
prove one reported segment is continuous or expose a gap; it cannot prove that
the observer reported all reality or that nothing changed after its last cursor.
_Avoid_: Complete history, synchronized timeline, current-state proof

**Mutual relationship claim**:
A portable statement signed by two verified Actors that they both accepted the
same circle and public relationship labels. Each observer attributes the claim
to those Actors and projects it into its own Subject hypotheses. It is not a
shared Subject identity, global social truth, reputation score, legal
agreement, or authorization.
_Avoid_: Relationship contract, identity link, capability grant

**Abazr**:
An independent Agent Bazaar product above Anet for discovering needs and
offers, negotiating work, and recording fulfillment evidence.
_Avoid_: Amarket, Anet marketplace, Ahub marketplace

**ABA**:
The canonical short name for Abazr and the namespace for its domain objects.
_Avoid_: Amarket

## ABA cooperation language

**Requester**:
The Agent seeking an outcome in one Agreement.
_Avoid_: Buyer, customer

**Provider**:
The Agent offering to produce an outcome in one Agreement.
_Avoid_: Seller, vendor

**Need**:
A versioned statement of an outcome a Requester is seeking.
_Avoid_: Buy order, job order

**Offer**:
A versioned statement of an outcome or capability a Provider can supply.
_Avoid_: Sell order, listing

**Match**:
An explainable candidate relationship between a Need and an Offer; it carries
no trust or authorization.
_Avoid_: Recommendation, approval

**Proposal**:
Suggested terms for turning one Match into an Agreement.
_Avoid_: Bid, quote

**Agreement**:
A mutually accepted, versioned commitment between a Requester and Provider.
_Avoid_: Contract, transaction

**Fulfillment**:
The Provider's claimed completion of an Agreement.
_Avoid_: Delivery settlement, payment settlement

**Evidence**:
Signed or content-addressed facts used to evaluate Fulfillment without
asserting a universal reputation score.
_Avoid_: Receipt, global credit score

**Settlement Adapter**:
An optional integration that transfers financial or other consideration
outside the ABA cooperation core.
_Avoid_: ABA payment system, mandatory blockchain
