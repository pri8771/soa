"""Shared model-output → :class:`ExtractionResult` parsing (AIO-008).

Every model-based extraction adapter — the local OpenAI-compatible one
(AIO-007) and the hosted Claude one (AIO-008) — receives the SAME
documented JSON shape back from the model (the shape the AIO-011 system
prompt mandates) and must turn it into an :class:`ExtractionResult`
under the SAME honesty rules. Rather than let each adapter re-implement
(and drift on) that mapping, they share this one function.

The honesty rules encoded here are the contract's, not any one vendor's:

- a value for a field that was never requested is dropped with a warning
  — never passed along;
- evidence is page-level only (full-page polygon plus the model's quote):
  a language model has no geometry, and inventing boxes would be
  fabrication (quote→coordinate resolution is AIO-014's job). A page
  number the request does not contain yields NO evidence;
- confidence is the model's own uncalibrated self-report, clamped to
  [0, 1]; downstream policy (PRC-011) treats it as one signal;
- a field the model omitted is returned with ``raw_value=None`` — absence
  is explicit.

Reasons on :class:`ModelOutputInvalidError` stay generic on purpose: they
travel back to the model as the AIO-012 repair hint and into logs, so
model output over customer data must never be interpolated into them.
"""

import json

from soa_worker.extraction.provider import (
    EvidenceSpan,
    ExtractedField,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
)
from soa_worker.model_request_builder import BuiltModelRequest
from soa_worker.model_usage import ProviderCallUsage, ProviderUsage

__all__ = [
    "MAX_FIELD_ENTRIES",
    "MAX_VALUE_LENGTH",
    "ModelOutputInvalidError",
    "parse_model_extraction",
]

#: Hard bounds on model output, enforced regardless of whether the
#: provider honored ``max_tokens``. A response past either is rejected as
#: invalid so it flows through the AIO-012 repair path instead of landing
#: an unbounded row in the database. Generous enough that a legitimate
#: sales order never trips them.
MAX_FIELD_ENTRIES = 1000
MAX_VALUE_LENGTH = 20_000


class ModelOutputInvalidError(ExtractionProviderError):
    """The model answered, but not in the documented shape. Retryable —
    and specifically REPAIRABLE: the AIO-012 policy re-asks with the
    reason, bounded by attempt and cost ceilings. ``reason`` is safe to
    send back to the model and to log; ``cost_cents`` is what the failed
    call cost (failed calls still burn tokens)."""

    def __init__(
        self,
        reason: str,
        *,
        cost_cents: int = 0,
        usage: ProviderUsage | None = None,
        pricing_reference: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.reason = reason
        self.cost_cents = cost_cents
        self.usage = usage
        self.pricing_reference = pricing_reference
        self.call_usage = (
            ProviderCallUsage(
                provider=provider,
                model=model,
                usage=usage,
                estimated_cost_cents=cost_cents,
                pricing_reference=pricing_reference,
                outcome="invalid_output",
            )
            if provider is not None
            else None
        )
        super().__init__(
            f"the model returned output that does not match the expected shape ({reason})",
            retryable=True,
            usage_records=((self.call_usage,) if self.call_usage is not None else ()),
            usage_records_complete=True,
        )


def parse_model_extraction(
    request: ExtractionRequest,
    content: str,
    built: BuiltModelRequest,
    *,
    provider: str,
    model: str,
    cost_cents: int = 0,
    usage: ProviderUsage | None = None,
    pricing_reference: str | None = None,
    lead_warnings: tuple[str, ...] = (),
) -> ExtractionResult:
    """Turn one model response (``content``, the raw JSON text) into an
    :class:`ExtractionResult` for ``provider``/``model``. ``lead_warnings``
    are prepended before the builder's own warnings — e.g. a per-provider
    capability caveat."""
    try:
        parsed = json.loads(content)
    except ValueError:
        raise ModelOutputInvalidError(
            "the response was not valid JSON",
            cost_cents=cost_cents,
            usage=usage,
            pricing_reference=pricing_reference,
            provider=provider,
            model=model,
        ) from None
    if not isinstance(parsed, dict) or "fields" not in parsed:
        raise ModelOutputInvalidError(
            'the JSON object is missing the "fields" key',
            cost_cents=cost_cents,
            usage=usage,
            pricing_reference=pricing_reference,
            provider=provider,
            model=model,
        )
    entries = parsed["fields"]
    if not isinstance(entries, list):
        raise ModelOutputInvalidError(
            '"fields" must be a JSON array',
            cost_cents=cost_cents,
            usage=usage,
            pricing_reference=pricing_reference,
            provider=provider,
            model=model,
        )
    if len(entries) > MAX_FIELD_ENTRIES:
        raise ModelOutputInvalidError(
            f'"fields" has more than the maximum of {MAX_FIELD_ENTRIES} entries',
            cost_cents=cost_cents,
            usage=usage,
            pricing_reference=pricing_reference,
            provider=provider,
            model=model,
        )

    requested = {spec.key for spec in request.fields}
    pages = {page.page_number: page for page in request.pages}
    warnings = [*lead_warnings, *built.warnings]
    found: dict[tuple[str, int | None], ExtractedField] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "key" not in entry:
            continue
        key = str(entry["key"])
        if key not in requested:
            warnings.append(f"the model returned unrequested field {key!r}; dropped")
            continue
        value = entry.get("value")
        row_index = entry.get("row_index")
        row = int(row_index) if isinstance(row_index, int) and row_index >= 0 else None
        if value is None:
            continue  # absent: handled below with the requested sweep
        if len(str(value)) > MAX_VALUE_LENGTH:
            raise ModelOutputInvalidError(
                f"a field value exceeds the maximum length of {MAX_VALUE_LENGTH} characters",
                cost_cents=cost_cents,
                usage=usage,
                pricing_reference=pricing_reference,
                provider=provider,
                model=model,
            )
        confidence = entry.get("confidence")
        numeric = float(confidence) if isinstance(confidence, int | float) else 0.5
        evidence: tuple[EvidenceSpan, ...] = ()
        page_number = entry.get("page_number")
        page = pages.get(page_number) if isinstance(page_number, int) else None
        if page is not None:
            quote = entry.get("quote")
            evidence = (
                EvidenceSpan(
                    page_number=page.page_number,
                    polygon=(
                        (0.0, 0.0),
                        (float(page.width_px), 0.0),
                        (float(page.width_px), float(page.height_px)),
                        (0.0, float(page.height_px)),
                    ),
                    quote=str(quote) if isinstance(quote, str) and quote else None,
                ),
            )
        found[(key, row)] = ExtractedField(
            field_key=key,
            raw_value=str(value),
            confidence=min(max(numeric, 0.0), 1.0),
            row_index=row,
            evidence=evidence,
        )

    results = list(found.values())
    answered_keys = {key for key, _ in found}
    for spec in request.fields:
        if spec.key not in answered_keys:
            results.append(ExtractedField(field_key=spec.key, raw_value=None, confidence=0.0))
    return ExtractionResult(
        provider=provider,
        fields=tuple(results),
        model=model,
        cost_cents=cost_cents,
        usage=usage,
        pricing_reference=pricing_reference,
        warnings=tuple(warnings),
        instruction_reference=built.instruction_reference,
        usage_records=(
            ProviderCallUsage(
                provider=provider,
                model=model,
                usage=usage,
                estimated_cost_cents=cost_cents,
                pricing_reference=pricing_reference,
                outcome="succeeded",
            ),
        ),
    )
