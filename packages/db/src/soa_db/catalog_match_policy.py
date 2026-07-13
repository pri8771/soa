"""Matching policy and confidence integration (CAT-009).

Combines the deterministic tiers (CAT-006) and weighted fuzzy scoring
(CAT-007) under a per-FIELD-TYPE policy and produces one of three
honest outcomes:

- ``auto_matched`` — an UNAMBIGUOUS exact/normalized hit, or a fuzzy
  top candidate at/above the field type's auto-match threshold with a
  decisive lead over the runner-up; the decision carries the full
  reason chain;
- ``needs_review`` — ambiguity, or fuzzy candidates worth showing; the
  reviewer picks (REV-010/CAT-010 surface this);
- ``no_match`` — nothing plausible; stated, never guessed.

Everything needed to audit or reproduce the decision is RETAINED
serializably (``MatchDecision.to_record``): the selected record, every
candidate's per-feature scores, the tier, the fuzzy configuration
fingerprint, and the reasons. A reviewer override becomes a LABELED
EVALUATION EXAMPLE (``override_to_evaluation_example``) — the human's
choice, the machine's choice, and every rejected candidate — ready to
join a gold dataset (AIO-015) so matching quality is measured against
real corrections.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from soa_db.catalog_fuzzy import FuzzyCandidate, FuzzyConfig, fuzzy_match
from soa_db.catalog_matching import CatalogMatchIndex, MatchCandidate, RecordFacts

FIELD_TYPES = ("customer", "ship_to", "material", "uom", "generic")


class MatchPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class FieldMatchPolicy:
    """Thresholds for one field type."""

    #: Fuzzy score required to auto-match without a human.
    auto_match_min: float = 0.92
    #: Minimum lead over the runner-up for a fuzzy auto-match.
    min_lead: float = 0.05
    fuzzy: FuzzyConfig = field(default_factory=FuzzyConfig)

    def __post_init__(self) -> None:
        if not 0 <= self.auto_match_min <= 1:
            raise MatchPolicyError("auto_match_min must be within [0, 1]")
        if self.auto_match_min < self.fuzzy.accept_threshold:
            raise MatchPolicyError(
                "auto_match_min below the fuzzy accept threshold would auto-match "
                "candidates the scorer itself only suggests"
            )
        if self.min_lead < 0:
            raise MatchPolicyError("min_lead cannot be negative")


@dataclass(frozen=True)
class MatchPolicySet:
    """Per-field-type policies; unknown field types fail closed."""

    policies: dict[str, FieldMatchPolicy] = field(
        default_factory=lambda: {field_type: FieldMatchPolicy() for field_type in FIELD_TYPES}
    )

    def for_field_type(self, field_type: str) -> FieldMatchPolicy:
        try:
            return self.policies[field_type]
        except KeyError:
            raise MatchPolicyError(
                f"no match policy for field type {field_type!r} — "
                f"configured: {', '.join(sorted(self.policies))}"
            ) from None


@dataclass(frozen=True)
class MatchDecision:
    query: str
    field_type: str
    outcome: str  # auto_matched | needs_review | no_match
    selected_source_id: str | None
    #: Exact-tier candidates (when the tiers hit) or fuzzy candidates.
    tier: str | None
    exact_candidates: tuple[MatchCandidate, ...]
    fuzzy_candidates: tuple[FuzzyCandidate, ...]
    config_fingerprint: str
    reasons: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        """The serializable retention record: the selected record AND
        every feature score, ready to store on the extracted field."""
        return {
            "query": self.query,
            "field_type": self.field_type,
            "outcome": self.outcome,
            "selected_source_id": self.selected_source_id,
            "tier": self.tier,
            "config_fingerprint": self.config_fingerprint,
            "reasons": list(self.reasons),
            "exact_candidates": [
                {
                    "source_id": candidate.source_id,
                    "tier": candidate.tier,
                    "reason": candidate.reason,
                }
                for candidate in self.exact_candidates
            ],
            "fuzzy_candidates": [
                {
                    "source_id": candidate.source_id,
                    "total_score": candidate.total_score,
                    "verdict": candidate.verdict,
                    "features": [
                        {
                            "feature": score.feature,
                            "weight": score.weight,
                            "score": score.score,
                            "detail": score.detail,
                        }
                        for score in candidate.features
                    ],
                }
                for candidate in self.fuzzy_candidates
            ],
        }


def resolve_match(
    query: str,
    records: list[RecordFacts],
    *,
    field_type: str,
    policy_set: MatchPolicySet | None = None,
    as_of: date | None = None,
) -> MatchDecision:
    """Run the full matching stack for one extracted value."""
    policies = policy_set or MatchPolicySet()
    policy = policies.for_field_type(field_type)
    reasons: list[str] = []

    exact = CatalogMatchIndex(records, as_of=as_of).match(query)
    reasons.extend(exact.notes)
    if exact.candidates:
        unambiguous = exact.unambiguous
        if unambiguous is not None:
            reasons.append(f"auto-matched at the {exact.tier} tier: {unambiguous.reason}")
            return MatchDecision(
                query=query,
                field_type=field_type,
                outcome="auto_matched",
                selected_source_id=unambiguous.source_id,
                tier=exact.tier,
                exact_candidates=exact.candidates,
                fuzzy_candidates=(),
                config_fingerprint=policy.fuzzy.fingerprint,
                reasons=tuple(reasons),
            )
        reasons.append(
            f"{len(exact.candidates)} records match at the {exact.tier} tier — "
            "a human picks; ambiguity never auto-matches"
        )
        return MatchDecision(
            query=query,
            field_type=field_type,
            outcome="needs_review",
            selected_source_id=None,
            tier=exact.tier,
            exact_candidates=exact.candidates,
            fuzzy_candidates=(),
            config_fingerprint=policy.fuzzy.fingerprint,
            reasons=tuple(reasons),
        )

    fuzzy = fuzzy_match(query, records, policy.fuzzy)
    reasons.extend(fuzzy.notes)
    if not fuzzy.candidates:
        reasons.append("no exact, normalized, or fuzzy candidate — explicit no-match")
        return MatchDecision(
            query=query,
            field_type=field_type,
            outcome="no_match",
            selected_source_id=None,
            tier=None,
            exact_candidates=(),
            fuzzy_candidates=(),
            config_fingerprint=policy.fuzzy.fingerprint,
            reasons=tuple(reasons),
        )

    top = fuzzy.candidates[0]
    runner_up = fuzzy.candidates[1].total_score if len(fuzzy.candidates) > 1 else 0.0
    lead = round(top.total_score - runner_up, 4)
    if top.total_score >= policy.auto_match_min and lead >= policy.min_lead:
        reasons.append(
            f"fuzzy auto-match: {top.source_id!r} scored {top.total_score} "
            f"(>= {policy.auto_match_min}) with a {lead} lead over the runner-up "
            f"(>= {policy.min_lead})"
        )
        outcome = "auto_matched"
        selected: str | None = top.source_id
    else:
        if top.total_score < policy.auto_match_min:
            reasons.append(
                f"top fuzzy score {top.total_score} is below the {field_type} "
                f"auto-match threshold {policy.auto_match_min} — routed to review"
            )
        else:
            reasons.append(
                f"lead over the runner-up is only {lead} (< {policy.min_lead}) — "
                "too close to auto-match; routed to review"
            )
        outcome = "needs_review"
        selected = None
    return MatchDecision(
        query=query,
        field_type=field_type,
        outcome=outcome,
        selected_source_id=selected,
        tier=None,
        exact_candidates=(),
        fuzzy_candidates=fuzzy.candidates,
        config_fingerprint=fuzzy.config_fingerprint,
        reasons=tuple(reasons),
    )


def override_to_evaluation_example(
    decision: MatchDecision,
    *,
    chosen_source_id: str | None,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    """Turn a reviewer's override into a labeled example the evaluation
    loop can learn from (AIO-015 gold shape adjacent). ``chosen_source_id``
    None means the reviewer confirmed there is NO correct record."""
    if not reason.strip():
        raise MatchPolicyError("an override needs a written reason")
    machine_choice = decision.selected_source_id
    return {
        "kind": "catalog_match_override",
        "query": decision.query,
        "field_type": decision.field_type,
        "machine_outcome": decision.outcome,
        "machine_selected_source_id": machine_choice,
        "human_selected_source_id": chosen_source_id,
        "agreed": machine_choice == chosen_source_id,
        "rejected_candidates": [
            candidate.source_id
            for candidate in decision.fuzzy_candidates
            if candidate.source_id != chosen_source_id
        ]
        + [
            candidate.source_id
            for candidate in decision.exact_candidates
            if candidate.source_id != chosen_source_id
        ],
        "config_fingerprint": decision.config_fingerprint,
        "actor_id": actor_id,
        "reason": reason,
    }


__all__ = [
    "FIELD_TYPES",
    "FieldMatchPolicy",
    "MatchDecision",
    "MatchPolicyError",
    "MatchPolicySet",
    "override_to_evaluation_example",
    "resolve_match",
]
