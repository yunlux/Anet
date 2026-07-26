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

**Subject hypothesis**:
One observer's revisable estimate of the latent entity behind one or more
Actors. It is local, probabilistic, and may be split, merged, or superseded as
evidence changes.
_Avoid_: Subject identity, Principal, account owner

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
