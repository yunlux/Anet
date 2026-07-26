from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from .actors import validate_actor_id
from .relationship_disclosures import (
    DisclosedRelationshipActivity,
    RelationshipDisclosureBook,
)


def _now_ms(now: int | None = None) -> int:
    return int(time.time() * 1000) if now is None else int(now)


def _activity_value(activity: DisclosedRelationshipActivity) -> dict[str, Any]:
    return activity.to_dict()


def _new_subject(subject_ref: str) -> dict[str, Any]:
    return {
        "subject_ref": subject_ref,
        "reported_state": "unknown",
        "actor_links": {},
        "reported_circle": None,
        "reported_context_trust": {},
        "interaction_stats": {},
        "transition_refs": set(),
        "source_activity_ids": [],
        "last_reported_ms": 0,
    }


class ReportedRelationshipViewProjector:
    """Derive one sender-attributed social view without local projection."""

    @classmethod
    def project(
        cls,
        book: RelationshipDisclosureBook,
        *,
        sender_actor_id: str,
        subject_ref: str = "",
        include_activities: bool = False,
        activity_limit: int = 100,
        now: int | None = None,
    ) -> dict[str, Any]:
        sender = validate_actor_id(sender_actor_id)
        selected_subject = str(subject_ref).strip()
        page_limit = int(activity_limit)
        if not 1 <= page_limit <= 500:
            raise ValueError("reported relationship activity limit must be 1-500")

        received = sorted(
            book.all(sender_actor_id=sender, limit=None),
            key=lambda item: (item.received_ms, item.packet_id),
        )
        activities: list[DisclosedRelationshipActivity] = []
        seen: dict[str, dict[str, Any]] = {}
        for item in received:
            for activity in item.disclosure.activities:
                rendered = _activity_value(activity)
                previous = seen.get(activity.activity_id)
                if previous is not None:
                    if previous != rendered:
                        raise ValueError(
                            "reported relationship activity ID has conflicting bodies"
                        )
                    continue
                seen[activity.activity_id] = rendered
                activities.append(activity)

        subjects: dict[str, dict[str, Any]] = {}
        actor_states: dict[str, dict[str, Any]] = {}
        for activity in activities:
            cls._fold(
                activity,
                subjects=subjects,
                actor_states=actor_states,
            )

        if selected_subject:
            if selected_subject not in subjects:
                raise KeyError(
                    f"unknown reported Subject hypothesis: {selected_subject}"
                )
            subjects = {selected_subject: subjects[selected_subject]}

        subject_values = [
            cls._render_subject(subjects[key], actor_states=actor_states)
            for key in sorted(subjects)
        ]
        current = _now_ms(now)
        first_received = received[0].received_ms if received else 0
        last_received = received[-1].received_ms if received else 0
        last_issued = max(
            (item.disclosure.issued_ms for item in received),
            default=0,
        )
        warnings = [
            "history-baseline-unknown",
            "cross-packet-append-continuity-unproven",
        ]
        if any(item.disclosure.has_more for item in received):
            warnings.append("sender-reported-undisclosed-remainder")
        if not received:
            warnings.append("no-disclosure-received")

        view: dict[str, Any] = {
            "version": 1,
            "type": "anet.reported-relationship-view.v1",
            "observer_actor_id": sender,
            "audience_actor_id": book.own_actor_id,
            "viewpoint": "sender-reported",
            "completeness": "partial-unknown",
            "subjects": subject_values,
            "actors": [
                {
                    "actor_id": actor_id,
                    "reported_state": actor_states[actor_id][
                        "reported_state"
                    ],
                    "actor_kind": actor_states[actor_id]["actor_kind"],
                    "proof_scopes": sorted(
                        actor_states[actor_id]["proof_scopes"]
                    ),
                    "source_activity_ids": actor_states[actor_id][
                        "source_activity_ids"
                    ],
                    "fact_boundary": "remote-observer-report",
                }
                for actor_id in sorted(actor_states)
            ],
            "provenance": {
                "authenticated_disclosures": len(received),
                "unique_activities": len(activities),
                "packet_ids": [item.packet_id for item in received],
                "disclosure_ids": [
                    item.disclosure.disclosure_id for item in received
                ],
                "cursor_heads": sorted(
                    {
                        item.disclosure.next_cursor
                        for item in received
                    }
                ),
                "first_received_ms": first_received,
                "last_received_ms": last_received,
                "last_issued_ms": last_issued,
                "age_since_receive_ms": (
                    max(0, current - last_received) if last_received else None
                ),
                "source_proof": "authenticated-encrypted-packet",
            },
            "warnings": warnings,
            "projection_into_local_relations": False,
            "authorization_effect": "none",
        }
        if include_activities:
            selected_activities = [
                activity.to_dict()
                for activity in activities
                if (
                    not selected_subject
                    or activity.subject_ref == selected_subject
                    or selected_subject
                    in dict(activity.details).get(
                        "replacement_subject_refs",
                        (),
                    )
                )
            ]
            view["activities"] = selected_activities[-page_limit:]
            view["activities_truncated"] = (
                len(selected_activities) > page_limit
            )
        return view

    @staticmethod
    def _fold(
        activity: DisclosedRelationshipActivity,
        *,
        subjects: dict[str, dict[str, Any]],
        actor_states: dict[str, dict[str, Any]],
    ) -> None:
        details = dict(activity.details)
        subject = None
        if activity.subject_ref:
            subject = subjects.setdefault(
                activity.subject_ref,
                _new_subject(activity.subject_ref),
            )
            subject["source_activity_ids"].append(activity.activity_id)
            subject["last_reported_ms"] = max(
                subject["last_reported_ms"],
                activity.occurred_ms,
            )

        if activity.activity_type.startswith("actor.") and activity.actor_id:
            actor = actor_states.setdefault(
                activity.actor_id,
                {
                    "reported_state": "unknown",
                    "actor_kind": "",
                    "proof_scopes": set(),
                    "source_activity_ids": [],
                },
            )
            actor["reported_state"] = str(
                details.get("actor_state", "observed")
            )
            if details.get("actor_kind"):
                actor["actor_kind"] = str(details["actor_kind"])
            if details.get("proof_scope"):
                actor["proof_scopes"].add(str(details["proof_scope"]))
            actor["source_activity_ids"].append(activity.activity_id)
            if subject is not None:
                subject["reported_state"] = (
                    "active"
                    if subject["reported_state"] == "unknown"
                    else subject["reported_state"]
                )
                subject["actor_links"].setdefault(
                    activity.actor_id,
                    {
                        "confidence": None,
                        "source_activity_id": activity.activity_id,
                    },
                )

        if (
            activity.activity_type == "subject.actor-linked"
            and subject is not None
            and activity.actor_id
        ):
            subject["reported_state"] = "active"
            subject["actor_links"][activity.actor_id] = {
                "confidence": details.get("confidence"),
                "source_activity_id": activity.activity_id,
            }

        if (
            activity.activity_type == "relationship.circle-set"
            and subject is not None
        ):
            subject["reported_state"] = "active"
            subject["reported_circle"] = {
                "circle": details.get("circle"),
                "confidence": details.get("confidence"),
                "source_activity_id": activity.activity_id,
                "reported_ms": activity.occurred_ms,
            }

        if (
            activity.activity_type == "relationship.context-trust-set"
            and subject is not None
        ):
            context = str(details.get("context", ""))
            if context:
                subject["reported_context_trust"][context] = {
                    "context": context,
                    "estimate": details.get("estimate"),
                    "confidence": details.get("confidence"),
                    "source_activity_id": activity.activity_id,
                    "reported_ms": activity.occurred_ms,
                }

        if (
            activity.activity_type == "interaction.observed"
            and subject is not None
        ):
            context = str(details.get("context", ""))
            direction = str(details.get("direction", ""))
            outcome = str(details.get("outcome", ""))
            for facet in details.get("facets", ()):
                key = (context, str(facet))
                stats = subject["interaction_stats"].setdefault(
                    key,
                    {
                        "context": context,
                        "facet": str(facet),
                        "incoming": 0,
                        "outgoing": 0,
                        "outcomes": defaultdict(int),
                    },
                )
                if direction in {"incoming", "outgoing"}:
                    stats[direction] += 1
                if outcome:
                    stats["outcomes"][outcome] += 1

        if activity.activity_type.startswith("subject.") and subject is not None:
            subject["reported_state"] = "superseded"
            transition_id = str(details.get("transition_id", ""))
            if transition_id:
                subject["transition_refs"].add(transition_id)
            for replacement_ref in details.get(
                "replacement_subject_refs",
                (),
            ):
                replacement = subjects.setdefault(
                    str(replacement_ref),
                    _new_subject(str(replacement_ref)),
                )
                replacement["reported_state"] = "active"
                if transition_id:
                    replacement["transition_refs"].add(transition_id)
                replacement["source_activity_ids"].append(
                    activity.activity_id
                )
                replacement["last_reported_ms"] = max(
                    replacement["last_reported_ms"],
                    activity.occurred_ms,
                )

    @staticmethod
    def _render_subject(
        subject: dict[str, Any],
        *,
        actor_states: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "subject_ref": subject["subject_ref"],
            "reported_state": subject["reported_state"],
            "actor_links": [
                {
                    "actor_id": actor_id,
                    "reported_actor_state": actor_states.get(
                        actor_id,
                        {},
                    ).get("reported_state", "unknown"),
                    **subject["actor_links"][actor_id],
                }
                for actor_id in sorted(subject["actor_links"])
            ],
            "reported_circle": subject["reported_circle"],
            "reported_context_trust": [
                subject["reported_context_trust"][key]
                for key in sorted(subject["reported_context_trust"])
            ],
            "interaction_stats": [
                {
                    **subject["interaction_stats"][key],
                    "outcomes": dict(
                        sorted(
                            subject["interaction_stats"][key][
                                "outcomes"
                            ].items()
                        )
                    ),
                }
                for key in sorted(subject["interaction_stats"])
            ],
            "transition_refs": sorted(subject["transition_refs"]),
            "source_activity_ids": subject["source_activity_ids"],
            "last_reported_ms": subject["last_reported_ms"],
            "fact_boundary": "remote-observer-report",
            "authorization_effect": "none",
        }
