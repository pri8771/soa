"""Weighted fuzzy catalog matching (CAT-007).

Ranks catalog records against an extracted value when the exact and
normalized tiers (CAT-006) found nothing. The scoring is transparent by
construction:

- **no unexplained aggregate** — a candidate's total is the weighted
  mean of its per-feature scores, and every feature travels on the
  candidate with its weight, score, and a human-readable detail naming
  the text it compared against; a test recomputes the total from the
  parts;
- **pinned configuration** — the weights, thresholds, and cap are a
  frozen config whose fingerprint is stamped on every result, so a
  stored match can name the exact scoring that produced it;
- **bounded and deterministic** — candidates are capped (with a note
  when the cap dropped anything) and tie-broken by source id.

Features:

- ``identifier`` — sequence similarity of normalized identifiers;
- ``text_trigram`` — Jaccard overlap of character trigrams of the
  normalized text, best across display name and aliases;
- ``text_sequence`` — difflib sequence similarity of the normalized
  text, best across display name and aliases.

Verdicts: ``accept`` at or above the accept threshold, ``suggest`` at
or above the suggest threshold, else the candidate is dropped — the
router/review flow decides what to do with suggestions; fuzzy matching
never auto-picks silently.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from soa_db.catalog_matching import RecordFacts, normalize_identifier, normalize_text

FEATURES = ("identifier", "text_trigram", "text_sequence")


class FuzzyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FuzzyConfig:
    weights: dict[str, float] = field(
        default_factory=lambda: {"identifier": 0.2, "text_trigram": 0.45, "text_sequence": 0.35}
    )
    accept_threshold: float = 0.85
    suggest_threshold: float = 0.5
    candidate_cap: int = 5

    def __post_init__(self) -> None:
        unknown = set(self.weights) - set(FEATURES)
        if unknown:
            raise FuzzyConfigError(f"unknown features: {sorted(unknown)} (known: {FEATURES})")
        if not self.weights or sum(self.weights.values()) <= 0:
            raise FuzzyConfigError("weights must be positive and sum above zero")
        if any(weight < 0 for weight in self.weights.values()):
            raise FuzzyConfigError("weights cannot be negative")
        if not 0 <= self.suggest_threshold <= self.accept_threshold <= 1:
            raise FuzzyConfigError(
                "thresholds must satisfy 0 <= suggest <= accept <= 1 — a suggestion "
                "bar above the accept bar is a contradiction"
            )
        if self.candidate_cap < 1:
            raise FuzzyConfigError("the candidate cap must be at least 1")

    @property
    def fingerprint(self) -> str:
        """Stable identity of this scoring configuration — stored with
        every match so it can be reproduced exactly."""
        canonical = json.dumps(
            {
                "weights": dict(sorted(self.weights.items())),
                "accept": self.accept_threshold,
                "suggest": self.suggest_threshold,
                "cap": self.candidate_cap,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FeatureScore:
    feature: str
    weight: float
    score: float
    detail: str


@dataclass(frozen=True)
class FuzzyCandidate:
    record_id: object
    source_id: str
    display_name: str
    total_score: float
    verdict: str  # accept | suggest
    features: tuple[FeatureScore, ...]


@dataclass(frozen=True)
class FuzzyMatchResult:
    query: str
    candidates: tuple[FuzzyCandidate, ...]
    config_fingerprint: str
    accept_threshold: float
    suggest_threshold: float
    notes: tuple[str, ...]


def _trigrams(text: str) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _trigram_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    left, right = _trigrams(a), _trigrams(b)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _sequence_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _best_text_score(
    query_norm: str, record: RecordFacts, metric: Callable[[str, str], float]
) -> tuple[float, str]:
    best_score, best_text = 0.0, record.display_name
    for text in (record.display_name, *record.aliases):
        score = metric(query_norm, normalize_text(text))
        if score > best_score:
            best_score, best_text = score, text
    return best_score, best_text


def fuzzy_match(
    query: str, records: list[RecordFacts], config: FuzzyConfig | None = None
) -> FuzzyMatchResult:
    """Score every record against the query. See module docstring."""
    effective = config or FuzzyConfig()
    query_norm_text = normalize_text(query)
    query_norm_id = normalize_identifier(query)
    weight_sum = sum(effective.weights.values())

    scored: list[FuzzyCandidate] = []
    for record in sorted(records, key=lambda r: r.source_id):
        features: list[FeatureScore] = []
        for feature, weight in sorted(effective.weights.items()):
            if feature == "identifier":
                score = _sequence_similarity(query_norm_id, normalize_identifier(record.source_id))
                detail = f"normalized identifiers {query_norm_id!r} vs {record.source_id!r}"
            elif feature == "text_trigram":
                score, text = _best_text_score(query_norm_text, record, _trigram_similarity)
                detail = f"trigram overlap with {text!r}"
            else:  # text_sequence
                score, text = _best_text_score(query_norm_text, record, _sequence_similarity)
                detail = f"sequence similarity with {text!r}"
            features.append(
                FeatureScore(feature=feature, weight=weight, score=round(score, 4), detail=detail)
            )
        total = round(sum(f.weight * f.score for f in features) / weight_sum, 4)
        if total < effective.suggest_threshold:
            continue
        verdict = "accept" if total >= effective.accept_threshold else "suggest"
        scored.append(
            FuzzyCandidate(
                record_id=record.record_id,
                source_id=record.source_id,
                display_name=record.display_name,
                total_score=total,
                verdict=verdict,
                features=tuple(features),
            )
        )

    ordered = sorted(scored, key=lambda c: (-c.total_score, c.source_id))
    notes: list[str] = []
    if len(ordered) > effective.candidate_cap:
        notes.append(
            f"{len(ordered) - effective.candidate_cap} candidates above the suggest "
            f"threshold were dropped by the cap of {effective.candidate_cap}"
        )
        ordered = ordered[: effective.candidate_cap]
    if not ordered:
        notes.append(
            f"no record scored at or above the suggest threshold ({effective.suggest_threshold})"
        )
    return FuzzyMatchResult(
        query=query,
        candidates=tuple(ordered),
        config_fingerprint=effective.fingerprint,
        accept_threshold=effective.accept_threshold,
        suggest_threshold=effective.suggest_threshold,
        notes=tuple(notes),
    )


__all__ = [
    "FEATURES",
    "FeatureScore",
    "FuzzyCandidate",
    "FuzzyConfig",
    "FuzzyConfigError",
    "FuzzyMatchResult",
    "fuzzy_match",
]
