from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .encoding import canonical_pack


DISCORD_SIGNAL_KIND = "social.discord.signal"
SOCIAL_PROTOCOL = "anet.social.discord"
SOCIAL_VERSION = 1
SOCIAL_POLICY_VERSION = 1
SOCIAL_ACTIONS = (
    "observe",
    "surface",
    "reply",
    "amplify",
    "connect_candidate",
)
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(
    r"^[a-z][a-z0-9_.-]{0,31}:[a-z0-9][a-z0-9_.-]{0,63}$"
)
_MANUAL_LABEL_PREFIXES = frozenset(
    {
        "community",
        "interest",
        "language",
        "relationship",
        "risk",
        "status",
    }
)
_BLOCKING_LABELS = frozenset(
    {
        "risk:block",
        "risk:impersonation",
        "risk:malware",
        "risk:spam",
    }
)


def normalize_social_label(value: str, *, manual: bool = False) -> str:
    label = str(value).strip().lower()
    if not _LABEL_RE.fullmatch(label):
        raise ValueError("invalid social label")
    if manual and label.partition(":")[0] not in _MANUAL_LABEL_PREFIXES:
        raise ValueError("manual social label uses a reserved prefix")
    return label


@dataclass(frozen=True)
class SocialThreshold:
    min_score: int
    min_confidence: int
    required_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.min_score <= 100:
            raise ValueError("social threshold score must be between 0 and 100")
        if not 0 <= self.min_confidence <= 100:
            raise ValueError(
                "social threshold confidence must be between 0 and 100"
            )
        normalized = tuple(
            sorted(
                {
                    normalize_social_label(label)
                    for label in self.required_labels
                }
            )
        )
        object.__setattr__(self, "required_labels", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_score": self.min_score,
            "min_confidence": self.min_confidence,
            "required_labels": list(self.required_labels),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SocialThreshold:
        if not isinstance(value, Mapping):
            raise ValueError("social threshold must be an object")
        if set(value) != {
            "min_score",
            "min_confidence",
            "required_labels",
        }:
            raise ValueError("social threshold has unexpected fields")
        return cls(
            min_score=_exact_int(value["min_score"], "threshold min_score"),
            min_confidence=_exact_int(
                value["min_confidence"],
                "threshold min_confidence",
            ),
            required_labels=tuple(
                _string_list(
                    value["required_labels"],
                    "threshold required_labels",
                    maximum=16,
                )
            ),
        )


@dataclass(frozen=True)
class SocialPolicy:
    surface: SocialThreshold = SocialThreshold(45, 0)
    reply: SocialThreshold = SocialThreshold(60, 25)
    amplify: SocialThreshold = SocialThreshold(72, 50)
    connect_candidate: SocialThreshold = SocialThreshold(
        82,
        70,
        ("relationship:vouched",),
    )
    version: int = SOCIAL_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != SOCIAL_POLICY_VERSION:
            raise ValueError("unsupported social policy version")
        ordered = (
            self.surface.min_score,
            self.reply.min_score,
            self.amplify.min_score,
            self.connect_candidate.min_score,
        )
        if tuple(sorted(ordered)) != ordered:
            raise ValueError("social action score thresholds must be monotonic")
        confidence = (
            self.surface.min_confidence,
            self.reply.min_confidence,
            self.amplify.min_confidence,
            self.connect_candidate.min_confidence,
        )
        if tuple(sorted(confidence)) != confidence:
            raise ValueError(
                "social action confidence thresholds must be monotonic"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "surface": self.surface.to_dict(),
            "reply": self.reply.to_dict(),
            "amplify": self.amplify.to_dict(),
            "connect_candidate": self.connect_candidate.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SocialPolicy:
        if not isinstance(value, Mapping):
            raise ValueError("social policy must be an object")
        expected = {
            "version",
            "surface",
            "reply",
            "amplify",
            "connect_candidate",
        }
        if set(value) != expected:
            raise ValueError("social policy has unexpected fields")
        return cls(
            version=_exact_int(value["version"], "social policy version"),
            surface=SocialThreshold.from_dict(value["surface"]),
            reply=SocialThreshold.from_dict(value["reply"]),
            amplify=SocialThreshold.from_dict(value["amplify"]),
            connect_candidate=SocialThreshold.from_dict(
                value["connect_candidate"]
            ),
        )

    def evaluate(
        self,
        stats: Mapping[str, Any],
        labels: set[str] | frozenset[str],
        event_labels: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        normalized_labels = {
            normalize_social_label(label) for label in labels
        }
        normalized_event_labels = {
            normalize_social_label(label) for label in event_labels
        }
        combined = normalized_labels | normalized_event_labels
        reputation = score_social_actor(stats, normalized_labels)
        allowed = ["observe"]
        reasons = list(reputation["reasons"])
        blocked = sorted(combined & _BLOCKING_LABELS)
        actor_is_automation = bool(
            combined & {"actor:bot", "actor:webhook"}
        )

        if blocked:
            reasons.append(f"blocked by labels: {','.join(blocked)}")
        else:
            if self._passes(self.surface, reputation, combined):
                allowed.append("surface")
            if (
                "interaction:mention" in combined
                and not actor_is_automation
                and self._passes(self.reply, reputation, combined)
            ):
                allowed.append("reply")
            if (
                not actor_is_automation
                and self._passes(self.amplify, reputation, combined)
            ):
                allowed.append("amplify")
            if (
                not actor_is_automation
                and self._passes(
                    self.connect_candidate,
                    reputation,
                    combined,
                )
            ):
                allowed.append("connect_candidate")

        action = allowed[-1]
        if action == "connect_candidate":
            reasons.append(
                "candidate only; Discord evidence cannot create Anet trust"
            )
        return {
            "action": action,
            "allowed_actions": allowed,
            "reasons": reasons[:32],
            "policy_version": self.version,
            "reputation": reputation,
        }

    @staticmethod
    def _passes(
        threshold: SocialThreshold,
        reputation: Mapping[str, Any],
        labels: set[str],
    ) -> bool:
        return (
            int(reputation["score"]) >= threshold.min_score
            and int(reputation["confidence"]) >= threshold.min_confidence
            and set(threshold.required_labels).issubset(labels)
        )


def score_social_actor(
    stats: Mapping[str, Any],
    labels: set[str] | frozenset[str],
) -> dict[str, Any]:
    normalized_labels = {
        normalize_social_label(label) for label in labels
    }
    mentions = _bounded_stat(stats, "mention_count", 10_000)
    replies = _bounded_stat(stats, "reply_count", 10_000)
    reactions = _bounded_stat(stats, "reaction_count", 1_000_000)
    pinned = _bounded_stat(stats, "pinned_count", 10_000)
    account_age_days = _bounded_stat(
        stats,
        "account_age_days",
        100_000,
    )

    points = 0
    confidence = 0
    reasons: list[str] = []

    age_points = min(account_age_days // 365, 5)
    if age_points:
        points += age_points
        confidence += 5
        reasons.append(f"discord account age +{age_points}")

    mention_points = min(mentions * 2, 10)
    if mention_points:
        points += mention_points
        confidence += min(mentions * 3, 15)
        reasons.append(f"bounded mentions +{mention_points}")

    reply_points = min(replies * 4, 12)
    if reply_points:
        points += reply_points
        confidence += min(replies * 5, 20)
        reasons.append(f"bounded replies +{reply_points}")

    reaction_points = min(reactions, 8)
    if reaction_points:
        points += reaction_points
        confidence += min(reactions, 10)
        reasons.append(f"bounded reactions +{reaction_points}")

    pinned_points = min(pinned * 8, 16)
    if pinned_points:
        points += pinned_points
        confidence += min(pinned * 10, 20)
        reasons.append(f"bounded pinned messages +{pinned_points}")

    manual_weights = {
        "community:moderator": (10, 20),
        "relationship:known": (10, 20),
        "relationship:vouched": (30, 50),
        "status:verified": (15, 30),
        "risk:concern": (-30, 30),
        "risk:impersonation": (-80, 80),
        "risk:malware": (-100, 100),
        "risk:spam": (-60, 60),
        "risk:block": (-100, 100),
    }
    for label in sorted(normalized_labels):
        weight = manual_weights.get(label)
        if weight is None:
            continue
        points += weight[0]
        confidence += weight[1]
        reasons.append(f"operator label {label} {weight[0]:+d}")

    raw_score = max(0, min(100, 50 + points))
    confidence = max(0, min(100, confidence))
    score = round(50 + ((raw_score - 50) * confidence / 100))
    return {
        "score": score,
        "raw_score": raw_score,
        "confidence": confidence,
        "algorithm": "anet-social-evidence-v1",
        "reasons": reasons or ["no reputation evidence"],
    }


def build_discord_signal(
    *,
    source_event_id: str,
    actor_key: str,
    created_ms: int,
    expires_ms: int,
    content_level: str,
    content: str,
    labels: list[str] | tuple[str, ...] | set[str],
    evaluation: Mapping[str, Any],
    guild_key: str,
    channel_key: str,
    message_revision: str,
) -> dict[str, Any]:
    reputation = dict(evaluation["reputation"])
    decision = {
        "action": str(evaluation["action"]),
        "allowed_actions": list(evaluation["allowed_actions"]),
        "reasons": list(evaluation["reasons"]),
        "policy_version": _exact_int(
            evaluation["policy_version"],
            "policy_version",
        ),
    }
    seed = {
        "source_event_id": source_event_id,
        "actor_key": actor_key,
        "reputation": reputation,
        "decision": decision,
        "message_revision": message_revision,
    }
    signal_id = hashlib.blake2s(
        canonical_pack(seed),
        digest_size=16,
        person=b"anetsoc1",
    ).hexdigest()
    value = {
        "protocol": SOCIAL_PROTOCOL,
        "version": SOCIAL_VERSION,
        "signal_id": signal_id,
        "source_event_id": source_event_id,
        "created_ms": created_ms,
        "expires_ms": expires_ms,
        "actor_key": actor_key,
        "content_level": content_level,
        "content": content,
        "labels": sorted(
            {normalize_social_label(label) for label in labels}
        ),
        "reputation": {
            "score": reputation["score"],
            "raw_score": reputation["raw_score"],
            "confidence": reputation["confidence"],
            "algorithm": reputation["algorithm"],
        },
        "decision": decision,
        "provenance": {
            "platform": "discord",
            "adapter": "discord-rest-v10",
            "guild_key": guild_key,
            "channel_key": channel_key,
            "message_revision": message_revision,
        },
    }
    return validate_discord_signal(value)


def validate_discord_signal(value: Any) -> dict[str, Any]:
    body = _exact_object(
        value,
        {
            "protocol",
            "version",
            "signal_id",
            "source_event_id",
            "created_ms",
            "expires_ms",
            "actor_key",
            "content_level",
            "content",
            "labels",
            "reputation",
            "decision",
            "provenance",
        },
        "Discord social signal",
    )
    if body["protocol"] != SOCIAL_PROTOCOL:
        raise ValueError("invalid Discord social protocol")
    if _exact_int(body["version"], "social version") != SOCIAL_VERSION:
        raise ValueError("unsupported Discord social version")
    signal_id = _hex(body["signal_id"], 32, "signal_id")
    source_event_id = _hex(
        body["source_event_id"],
        32,
        "source_event_id",
    )
    actor_key = _hex(body["actor_key"], 64, "actor_key")
    created_ms = _positive_int(body["created_ms"], "created_ms")
    expires_ms = _positive_int(body["expires_ms"], "expires_ms")
    if not created_ms < expires_ms <= created_ms + 7 * 86_400_000:
        raise ValueError("invalid Discord social signal lifetime")
    content_level = _string(body["content_level"], "content_level")
    if content_level not in {"metadata", "mention"}:
        raise ValueError("invalid Discord social content level")
    content = _string(body["content"], "content")
    if len(content) > 2000:
        raise ValueError("Discord social content is too long")
    if content_level == "metadata" and content:
        raise ValueError("metadata-only Discord signal contains content")
    labels = sorted(
        {
            normalize_social_label(label)
            for label in _string_list(
                body["labels"],
                "labels",
                maximum=32,
            )
        }
    )
    if len(labels) != len(body["labels"]):
        raise ValueError("Discord social labels must be unique and sorted")

    reputation = _exact_object(
        body["reputation"],
        {"score", "raw_score", "confidence", "algorithm"},
        "Discord social reputation",
    )
    normalized_reputation = {
        "score": _percentage(reputation["score"], "reputation score"),
        "raw_score": _percentage(
            reputation["raw_score"],
            "reputation raw_score",
        ),
        "confidence": _percentage(
            reputation["confidence"],
            "reputation confidence",
        ),
        "algorithm": _string(reputation["algorithm"], "reputation algorithm"),
    }
    if normalized_reputation["algorithm"] != "anet-social-evidence-v1":
        raise ValueError("unsupported social reputation algorithm")

    decision = _exact_object(
        body["decision"],
        {"action", "allowed_actions", "reasons", "policy_version"},
        "Discord social decision",
    )
    action = _string(decision["action"], "decision action")
    if action not in SOCIAL_ACTIONS:
        raise ValueError("invalid social decision action")
    allowed_actions = _string_list(
        decision["allowed_actions"],
        "allowed_actions",
        maximum=len(SOCIAL_ACTIONS),
    )
    if (
        not allowed_actions
        or allowed_actions[0] != "observe"
        or action != allowed_actions[-1]
        or any(item not in SOCIAL_ACTIONS for item in allowed_actions)
        or allowed_actions != list(dict.fromkeys(allowed_actions))
    ):
        raise ValueError("invalid social allowed action sequence")
    reasons = _string_list(
        decision["reasons"],
        "decision reasons",
        maximum=32,
        item_limit=256,
    )
    policy_version = _exact_int(
        decision["policy_version"],
        "policy_version",
    )
    if policy_version != SOCIAL_POLICY_VERSION:
        raise ValueError("unsupported social policy version")

    provenance = _exact_object(
        body["provenance"],
        {
            "platform",
            "adapter",
            "guild_key",
            "channel_key",
            "message_revision",
        },
        "Discord social provenance",
    )
    if provenance["platform"] != "discord":
        raise ValueError("invalid social platform")
    if provenance["adapter"] != "discord-rest-v10":
        raise ValueError("unsupported Discord social adapter")
    guild_key = _hex(provenance["guild_key"], 64, "guild_key")
    channel_key = _hex(provenance["channel_key"], 64, "channel_key")
    message_revision = _string(
        provenance["message_revision"],
        "message_revision",
    )
    if not 1 <= len(message_revision) <= 64:
        raise ValueError("invalid Discord message revision")

    return {
        "protocol": SOCIAL_PROTOCOL,
        "version": SOCIAL_VERSION,
        "signal_id": signal_id,
        "source_event_id": source_event_id,
        "created_ms": created_ms,
        "expires_ms": expires_ms,
        "actor_key": actor_key,
        "content_level": content_level,
        "content": content,
        "labels": labels,
        "reputation": normalized_reputation,
        "decision": {
            "action": action,
            "allowed_actions": allowed_actions,
            "reasons": reasons,
            "policy_version": policy_version,
        },
        "provenance": {
            "platform": "discord",
            "adapter": "discord-rest-v10",
            "guild_key": guild_key,
            "channel_key": channel_key,
            "message_revision": message_revision,
        },
    }


def _bounded_stat(stats: Mapping[str, Any], name: str, maximum: int) -> int:
    value = _exact_int(stats.get(name, 0), name)
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _exact_object(
    value: Any,
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    missing = fields - set(value)
    extra = set(value) - fields
    if missing:
        raise ValueError(f"{label} is missing fields: {','.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{label} has unknown fields: {','.join(sorted(extra))}")
    return value


def _exact_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _exact_int(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _percentage(value: Any, label: str) -> int:
    result = _exact_int(value, label)
    if not 0 <= result <= 100:
        raise ValueError(f"{label} must be between 0 and 100")
    return result


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    maximum: int,
    item_limit: int = 128,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded list")
    result = []
    for item in value:
        text = _string(item, label)
        if not text or len(text) > item_limit:
            raise ValueError(f"{label} contains invalid text")
        result.append(text)
    return result


def _hex(value: Any, length: int, label: str) -> str:
    text = _string(value, label).strip().lower()
    pattern = _HEX32_RE if length == 32 else _HEX64_RE
    if not pattern.fullmatch(text):
        raise ValueError(f"{label} must be {length} lowercase hexadecimal characters")
    return text
