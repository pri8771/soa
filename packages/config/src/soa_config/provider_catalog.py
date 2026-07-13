"""The shipped provider catalog (AIO-019).

ONE declaration of every document-AI adapter the platform ships, shared
by both sides: the worker registers its runtime providers from the same
facts (a worker test cross-checks the live registry against this
catalog, so drift fails CI), and the API serves it to the provider
administration UI without importing worker code.

Health is deliberately NOT here: it is runtime state only the worker
knows. The API reports it as unknown until a worker-telemetry surface
exists — an honest gap, not a guess.

``routing_preview`` mirrors the worker router's elimination semantics
(AIO-013) over the static catalog so administrators can see how a
policy would order providers. It is a PREVIEW: the authoritative
decision happens in the worker with live health/quality/cost signals,
and a worker test keeps the two orderings aligned for the catalog pool.
"""

from dataclasses import dataclass

ANY_LANGUAGE = "*"


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    capability: str  # native_text | ocr | classify | split | field_extraction
    languages: tuple[str, ...]
    processing_region: str  # "local" or a hosted region slug
    sends_content_to_third_party: bool
    retains_content: bool
    uses_content_for_training: bool
    #: What must be true for this adapter to register at runtime.
    availability: str  # always | requires_binary:<name> | requires_endpoint_config
    description: str

    @property
    def local(self) -> bool:
        return self.processing_region == "local"


PROVIDER_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name="mock",
        capability="field_extraction",
        languages=(ANY_LANGUAGE,),
        processing_region="local",
        sends_content_to_third_party=False,
        retains_content=False,
        uses_content_for_training=False,
        availability="always",
        description="Deterministic fixture-based extraction (development and tests).",
    ),
    CatalogEntry(
        name="pdfium-native-text",
        capability="native_text",
        languages=(ANY_LANGUAGE,),
        processing_region="local",
        sends_content_to_third_party=False,
        retains_content=False,
        uses_content_for_training=False,
        availability="always",
        description="Sandboxed digital-PDF text extraction with viewer-matched coordinates.",
    ),
    CatalogEntry(
        name="tesseract",
        capability="ocr",
        languages=("de", "en"),  # the packs the deployment installs
        processing_region="local",
        sends_content_to_third_party=False,
        retains_content=False,
        uses_content_for_training=False,
        availability="requires_binary:tesseract",
        description="Local OCR with word/line/block geometry and per-word confidence.",
    ),
    CatalogEntry(
        name="local-openai-compatible",
        capability="field_extraction",
        languages=(ANY_LANGUAGE,),
        processing_region="local",
        sends_content_to_third_party=False,
        retains_content=False,
        uses_content_for_training=False,
        availability="requires_endpoint_config",
        description=(
            "Optional local LLM extraction (llama.cpp/vLLM/Ollama); registers only "
            "when the deployment configures an endpoint."
        ),
    ),
)


def catalog_warnings(entry: CatalogEntry) -> tuple[str, ...]:
    """The explicit data-policy warnings the admin UI must show."""
    warnings: list[str] = []
    if entry.sends_content_to_third_party:
        warnings.append("Customer content LEAVES the deployment to a third party.")
    if entry.retains_content:
        warnings.append("The vendor RETAINS customer content after processing.")
    if entry.uses_content_for_training:
        warnings.append("The vendor may TRAIN on customer content.")
    if not warnings:
        warnings.append("Runs inside the deployment; content never leaves (local-only safe).")
    return tuple(warnings)


def routing_preview(
    capability: str,
    *,
    local_only: bool = False,
    language: str | None = None,
    allow_third_party_processing: bool = False,
    preferred_order: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(ordered provider names, explanation lines) — the AIO-013
    elimination semantics over the static catalog."""
    explanation: list[str] = []
    pool = [entry for entry in PROVIDER_CATALOG if entry.capability == capability]
    explanation.append(
        f"catalog providers for {capability}: {', '.join(e.name for e in pool) or '(none)'}"
    )
    if language is not None:
        kept = [e for e in pool if ANY_LANGUAGE in e.languages or language.lower() in e.languages]
        for entry in pool:
            if entry not in kept:
                explanation.append(f"eliminated {entry.name}: no {language!r} support")
        pool = kept
    if local_only:
        kept = [e for e in pool if e.local]
        for entry in pool:
            if entry not in kept:
                explanation.append(f"eliminated {entry.name}: the policy is local-only")
        pool = kept
    elif not allow_third_party_processing:
        kept = [e for e in pool if not e.sends_content_to_third_party]
        for entry in pool:
            if entry not in kept:
                explanation.append(
                    f"eliminated {entry.name}: third-party processing is not allowed"
                )
        pool = kept

    def rank(entry: CatalogEntry) -> tuple[int, str]:
        preferred = (
            preferred_order.index(entry.name)
            if entry.name in preferred_order
            else len(preferred_order)
        )
        return (preferred, entry.name)

    ordered = tuple(entry.name for entry in sorted(pool, key=rank))
    explanation.append(
        "preview order (static catalog; live health/quality/cost apply at runtime): "
        + (", ".join(ordered) or "(none)")
    )
    return ordered, tuple(explanation)


__all__ = [
    "ANY_LANGUAGE",
    "PROVIDER_CATALOG",
    "CatalogEntry",
    "catalog_warnings",
    "routing_preview",
]
