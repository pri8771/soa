"""Provider router (AIO-013).

Chooses which provider serves a capability for one document, as a pure,
deterministic function of the registry plus explicit inputs — the
resolved tenant routing policy (CFG-005/006), document facts, and the
operational signals (health, quality, cost estimates). The SAME inputs
always produce the SAME decision and the SAME explanation: every
elimination and ranking step is recorded in order, so an operator can
read exactly why a provider was or was not used.

Hard rules:

- **local-only is honoured absolutely** — a policy with
  ``local_only=True`` can never route to a provider whose data policy
  leaves the deployment, whatever quality or preference says;
- **the budget cannot be exceeded silently** — a candidate whose cost
  estimate exceeds the remaining document budget is skipped with an
  explanation, and if NO candidate fits, routing raises
  :class:`BudgetExceededError` instead of quietly overspending;
- **fail closed** — no candidates after filtering is an explicit
  :class:`NoRouteError` carrying the full explanation, never a default.

``route_text_capability`` handles the native/scanned fork: documents
whose native text coverage is good use the native-text provider;
scanned or image documents go to OCR.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from soa_worker.providers.capabilities import (
    Capability,
    ProviderInfo,
    select_providers,
)

#: Native text below this coverage routes the document to OCR.
NATIVE_COVERAGE_THRESHOLD = 0.6


@dataclass(frozen=True)
class RoutingPolicy:
    """The tenant's resolved routing policy for one stream."""

    #: Content must never leave the deployment.
    local_only: bool = False
    #: Hosted processing regions the tenant allows (None = any region,
    #: subject to the allow_* flags below).
    allowed_regions: tuple[str, ...] | None = None
    allow_third_party_processing: bool = False
    allow_content_retention: bool = False
    allow_training_on_content: bool = False
    #: Exact providers this immutable policy permits. ``None`` preserves
    #: catalog-wide routing for previews and legacy callers; production run
    #: resolution supplies the pinned primary + fallback chain here so the
    #: router can never select an adapter whose credentials were not pinned.
    allowed_providers: tuple[str, ...] | None = None
    #: Explicit fallback order: providers named here are tried first,
    #: in this order; unnamed eligible providers follow by rank.
    preferred_order: tuple[str, ...] = ()
    #: Per-document budget for this capability; None = unmetered.
    budget_cents: int | None = None
    #: Immutable evaluation scores pinned by the provider policy. Operational
    #: confidence is useful telemetry, but it is not ground-truth accuracy and
    #: therefore cannot satisfy a non-zero quality gate.
    evaluated_quality_scores: Mapping[str, float] = field(default_factory=dict)
    #: Providers whose pinned evaluated score is below this are dropped.
    min_quality: float = 0.0


@dataclass(frozen=True)
class DocumentFacts:
    """What is known about the document being routed."""

    language: str | None = None
    #: Fraction of native text captured (AIO-002 coverage); None when
    #: native text was not attempted or not applicable.
    native_text_coverage: float | None = None


@dataclass(frozen=True)
class OperationalSignals:
    """Injected runtime signals; absent entries are handled fail-safe
    (an absent health entry preserves legacy ``ok`` behavior; callers with a
    durable health surface pass ``unknown`` explicitly so it ranks last;
    unknown quality ranks last, unknown
    cost treated as free only for LOCAL providers — hosted providers
    without an estimate are skipped when a budget applies)."""

    health: Mapping[str, str] = field(default_factory=dict)  # ok|degraded|unknown|unreachable
    # Live 0..1 ranking signal (for example observed confidence). A non-zero
    # quality gate uses RoutingPolicy.evaluated_quality_scores instead.
    quality: Mapping[str, float] = field(default_factory=dict)
    cost_cents: Mapping[str, int] = field(default_factory=dict)  # per-document estimate


@dataclass(frozen=True)
class RoutingDecision:
    provider: ProviderInfo
    #: Remaining eligible providers, in fallback order.
    fallbacks: tuple[str, ...]
    estimated_cost_cents: int
    explanation: tuple[str, ...]


class NoRouteError(Exception):
    def __init__(self, capability: Capability, explanation: list[str]) -> None:
        self.explanation = tuple(explanation)
        super().__init__(
            f"no {capability.value} provider satisfies the routing policy — "
            + " | ".join(explanation)
        )


class BudgetExceededError(Exception):
    """Every eligible provider costs more than the budget. Raised so the
    overrun is a decision someone makes, never a silent surprise."""

    def __init__(self, capability: Capability, budget_cents: int, explanation: list[str]) -> None:
        self.explanation = tuple(explanation)
        super().__init__(
            f"routing {capability.value} would exceed the {budget_cents} cent budget "
            "for every eligible provider — raise the budget or allow a cheaper provider"
        )


def route_text_capability(facts: DocumentFacts) -> tuple[Capability, str]:
    """The native/scanned fork: which text capability should read this
    document, and why."""
    coverage = facts.native_text_coverage
    if coverage is not None and coverage >= NATIVE_COVERAGE_THRESHOLD:
        return (
            Capability.NATIVE_TEXT,
            f"native text covers {coverage:.0%} (>= {NATIVE_COVERAGE_THRESHOLD:.0%}): "
            "digital document, no OCR needed",
        )
    reason = (
        "native text was not available"
        if coverage is None
        else f"native text covers only {coverage:.0%} (< {NATIVE_COVERAGE_THRESHOLD:.0%})"
    )
    return (Capability.OCR, f"{reason}: scanned/image document, routing to OCR")


def _estimate(info: ProviderInfo, signals: OperationalSignals) -> int | None:
    known = signals.cost_cents.get(info.name)
    if known is not None:
        return known
    # Local providers run on our own compute: free at the metering
    # boundary. A hosted provider without an estimate is UNKNOWN.
    return 0 if info.data_policy.processing_region == "local" else None


def route(
    capability: Capability,
    *,
    policy: RoutingPolicy,
    facts: DocumentFacts | None = None,
    signals: OperationalSignals | None = None,
) -> RoutingDecision:
    """Pick the provider for ``capability``. See module docstring."""
    effective_facts = facts or DocumentFacts()
    effective_signals = signals or OperationalSignals()
    explanation: list[str] = []

    # Under local-only, hosted providers still surface as candidates so
    # their elimination is EXPLICIT in the explanation below — the
    # decision is identical, the story is readable.
    candidates = list(
        select_providers(
            capability,
            language=effective_facts.language,
            allow_third_party_processing=policy.local_only or policy.allow_third_party_processing,
            allow_content_retention=policy.local_only or policy.allow_content_retention,
            allow_training_on_content=policy.local_only or policy.allow_training_on_content,
        )
    )
    language_note = f" (language {effective_facts.language!r})" if effective_facts.language else ""
    explanation.append(
        f"candidates for {capability.value}{language_note} under the data policy: "
        f"{', '.join(info.name for info in candidates) or '(none)'}"
    )

    if policy.allowed_providers is not None:
        allowed_names = set(policy.allowed_providers)
        kept = [info for info in candidates if info.name in allowed_names]
        for info in candidates:
            if info not in kept:
                explanation.append(
                    f"eliminated {info.name}: not present in the pinned provider chain"
                )
        candidates = kept

    if policy.local_only:
        kept = [info for info in candidates if info.data_policy.processing_region == "local"]
        for info in candidates:
            if info not in kept:
                explanation.append(f"eliminated {info.name}: the policy is local-only")
        candidates = kept

    if policy.allowed_regions is not None and not policy.local_only:
        allowed = {region.lower() for region in policy.allowed_regions} | {"local"}
        kept = [info for info in candidates if info.data_policy.processing_region in allowed]
        for info in candidates:
            if info not in kept:
                explanation.append(
                    f"eliminated {info.name}: region {info.data_policy.processing_region!r} "
                    f"is not among the allowed regions"
                )
        candidates = kept

    kept = []
    for info in candidates:
        status = effective_signals.health.get(info.name, "ok")
        if status not in {"ok", "degraded", "unknown", "unreachable"}:
            explanation.append(f"eliminated {info.name}: health signal is invalid")
            continue
        if status == "unreachable":
            explanation.append(f"eliminated {info.name}: health is unreachable")
            continue
        kept.append(info)
    candidates = kept

    kept = []
    for info in candidates:
        evaluated_score = policy.evaluated_quality_scores.get(info.name)
        score = (
            evaluated_score
            if evaluated_score is not None
            else effective_signals.quality.get(info.name)
        )
        if policy.min_quality > 0 and evaluated_score is None:
            explanation.append(
                f"eliminated {info.name}: evaluated quality is not pinned and the policy "
                f"requires a {policy.min_quality:.2f} floor"
            )
            continue
        if score is not None and score < policy.min_quality:
            explanation.append(
                f"eliminated {info.name}: quality {score:.2f} is below the "
                f"{policy.min_quality:.2f} floor"
            )
            continue
        kept.append(info)
    candidates = kept

    if not candidates:
        raise NoRouteError(capability, explanation)

    def rank(info: ProviderInfo) -> tuple[float, int, float, str]:
        preferred = (
            policy.preferred_order.index(info.name)
            if info.name in policy.preferred_order
            else len(policy.preferred_order)
        )
        health_rank = {
            "ok": 0,
            "degraded": 1,
            "unknown": 2,
        }[effective_signals.health.get(info.name, "ok")]
        quality = policy.evaluated_quality_scores.get(
            info.name, effective_signals.quality.get(info.name, -1.0)
        )
        return (preferred, health_rank, -quality, info.name)

    ordered = sorted(candidates, key=rank)
    explanation.append(
        "ranked (preference, health, quality, name): " + ", ".join(info.name for info in ordered)
    )

    affordable: list[tuple[ProviderInfo, int]] = []
    for info in ordered:
        estimate = _estimate(info, effective_signals)
        if policy.budget_cents is None:
            affordable.append((info, estimate or 0))
            continue
        if estimate is None:
            explanation.append(
                f"skipped {info.name}: no cost estimate and a budget applies — "
                "an unknown cost cannot be spent"
            )
            continue
        if estimate > policy.budget_cents:
            explanation.append(
                f"skipped {info.name}: estimated {estimate} cents exceeds the "
                f"{policy.budget_cents} cent budget"
            )
            continue
        affordable.append((info, estimate))

    if not affordable:
        assert policy.budget_cents is not None
        raise BudgetExceededError(capability, policy.budget_cents, explanation)

    chosen, cost = affordable[0]
    explanation.append(f"selected {chosen.name} (estimated {cost} cents)")
    return RoutingDecision(
        provider=chosen,
        fallbacks=tuple(info.name for info, _ in affordable[1:]),
        estimated_cost_cents=cost,
        explanation=tuple(explanation),
    )


__all__ = [
    "NATIVE_COVERAGE_THRESHOLD",
    "BudgetExceededError",
    "DocumentFacts",
    "NoRouteError",
    "OperationalSignals",
    "RoutingDecision",
    "RoutingPolicy",
    "route",
    "route_text_capability",
]
