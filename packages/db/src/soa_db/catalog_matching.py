"""Normalized and exact catalog matching (CAT-006).

Matches an extracted value ("the buyer wrote WIDGET 9") against a
catalog version's records, in DETERMINISTIC tiers with a written reason
for every candidate — a match nobody can explain is a match nobody
should trust:

1. ``exact_identifier`` — the query equals a record's source id verbatim;
2. ``normalized_identifier`` — identifiers compared case-insensitively
   with separators (space, dash, underscore, slash, dot) removed and
   leading zeros stripped, so ``sku-0009`` finds ``SKU-9``;
3. ``exact_text`` — the query equals the display name or an alias verbatim;
4. ``normalized_text`` — text compared casefolded, punctuation-free,
   whitespace-collapsed.

The FIRST tier with hits wins; candidates within a tier order by source
id, so the same query against the same records always yields the same
list. Ambiguity is surfaced (every record in the winning tier), never
resolved silently.

Date-effective filtering: an ``as_of`` date excludes records outside
their effective window BEFORE matching, and every exclusion is recorded
on the result — a reviewer sees that the right record exists but is out
of effect, instead of a mysterious no-match.

Scope: the index is built from records the caller loaded through the
tenant-scoped repository, normally via ``build_match_index`` which
resolves the stream's CAT-001 binding — tenant and stream scope come
from the platform's existing boundaries, not from this module trusting
its inputs.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.catalogs import (
    CatalogBinding,
    CatalogRecordRepository,
    resolve_catalog_version,
)
from soa_db.repository import OrganizationContext

_SEPARATORS = re.compile(r"[\s\-_/.]+")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

TIERS = ("exact_identifier", "normalized_identifier", "exact_text", "normalized_text")


def normalize_identifier(value: str) -> str:
    """Uppercase, separators removed, leading zeros of digit runs
    stripped: ``sku-0009`` -> ``SKU9``."""
    collapsed = _SEPARATORS.sub("", value.strip()).upper()
    return re.sub(r"0+(\d)", r"\1", collapsed)


def normalize_text(value: str) -> str:
    """Casefolded, punctuation-free, whitespace-collapsed."""
    without_punctuation = _PUNCTUATION.sub(" ", value)
    return _WHITESPACE.sub(" ", without_punctuation).strip().casefold()


@dataclass(frozen=True)
class RecordFacts:
    """The matching-relevant facts of one catalog record."""

    record_id: uuid.UUID
    source_id: str
    display_name: str
    aliases: tuple[str, ...]
    effective_from: date | None = None
    effective_to: date | None = None

    def effective_on(self, as_of: date) -> bool:
        if self.effective_from and as_of < self.effective_from:
            return False
        if self.effective_to and as_of > self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class MatchCandidate:
    record_id: uuid.UUID
    source_id: str
    display_name: str
    tier: str
    #: Which text on the record matched (the source id, the name, or an alias).
    matched_text: str
    reason: str


@dataclass(frozen=True)
class MatchResult:
    query: str
    tier: str | None  # the winning tier; None = no match
    candidates: tuple[MatchCandidate, ...]
    #: source ids excluded by date-effective filtering, with reasons.
    excluded: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def unambiguous(self) -> MatchCandidate | None:
        return self.candidates[0] if len(self.candidates) == 1 else None


class CatalogMatchIndex:
    """Indexed lookups over one catalog version's records. Build once,
    match many extracted values."""

    def __init__(self, records: list[RecordFacts], *, as_of: date | None = None) -> None:
        self._as_of = as_of
        self._excluded: list[str] = []
        self._notes: list[str] = []
        effective: list[RecordFacts] = []
        for record in sorted(records, key=lambda r: r.source_id):
            if as_of is not None and not record.effective_on(as_of):
                self._excluded.append(record.source_id)
                self._notes.append(
                    f"record {record.source_id!r} is out of effect on {as_of.isoformat()} "
                    "and was excluded from matching"
                )
                continue
            effective.append(record)

        self._by_exact_id: dict[str, list[RecordFacts]] = {}
        self._by_norm_id: dict[str, list[RecordFacts]] = {}
        self._by_exact_text: dict[str, list[tuple[RecordFacts, str]]] = {}
        self._by_norm_text: dict[str, list[tuple[RecordFacts, str]]] = {}
        for record in effective:
            self._by_exact_id.setdefault(record.source_id, []).append(record)
            self._by_norm_id.setdefault(normalize_identifier(record.source_id), []).append(record)
            for text in (record.display_name, *record.aliases):
                self._by_exact_text.setdefault(text, []).append((record, text))
                self._by_norm_text.setdefault(normalize_text(text), []).append((record, text))

    def match(self, query: str) -> MatchResult:
        stripped = query.strip()
        if not stripped:
            return MatchResult(
                query=query,
                tier=None,
                candidates=(),
                excluded=tuple(self._excluded),
                notes=(*self._notes, "an empty query matches nothing"),
            )

        candidates: list[MatchCandidate] = []
        tier: str | None = None

        for record in self._by_exact_id.get(stripped, []):
            candidates.append(
                MatchCandidate(
                    record_id=record.record_id,
                    source_id=record.source_id,
                    display_name=record.display_name,
                    tier="exact_identifier",
                    matched_text=record.source_id,
                    reason=f"the value equals source id {record.source_id!r} exactly",
                )
            )
        if candidates:
            tier = "exact_identifier"

        if tier is None:
            normalized = normalize_identifier(stripped)
            for record in self._by_norm_id.get(normalized, []):
                candidates.append(
                    MatchCandidate(
                        record_id=record.record_id,
                        source_id=record.source_id,
                        display_name=record.display_name,
                        tier="normalized_identifier",
                        matched_text=record.source_id,
                        reason=(
                            f"the value normalizes to {normalized!r}, the same as "
                            f"source id {record.source_id!r}"
                        ),
                    )
                )
            if candidates:
                tier = "normalized_identifier"

        if tier is None:
            for record, text in self._by_exact_text.get(stripped, []):
                via = "display name" if text == record.display_name else "alias"
                candidates.append(
                    MatchCandidate(
                        record_id=record.record_id,
                        source_id=record.source_id,
                        display_name=record.display_name,
                        tier="exact_text",
                        matched_text=text,
                        reason=f"the value equals the {via} {text!r} of {record.source_id!r}",
                    )
                )
            if candidates:
                tier = "exact_text"

        if tier is None:
            normalized_text_query = normalize_text(stripped)
            for record, text in self._by_norm_text.get(normalized_text_query, []):
                via = "display name" if text == record.display_name else "alias"
                candidates.append(
                    MatchCandidate(
                        record_id=record.record_id,
                        source_id=record.source_id,
                        display_name=record.display_name,
                        tier="normalized_text",
                        matched_text=text,
                        reason=(
                            f"the value and the {via} {text!r} of {record.source_id!r} "
                            f"both normalize to {normalized_text_query!r}"
                        ),
                    )
                )
            if candidates:
                tier = "normalized_text"

        # Deterministic order within the winning tier; a record matched
        # via several texts appears once (first text in record order).
        deduped: list[MatchCandidate] = []
        seen: set[uuid.UUID] = set()
        for candidate in sorted(candidates, key=lambda c: c.source_id):
            if candidate.record_id not in seen:
                seen.add(candidate.record_id)
                deduped.append(candidate)

        notes = list(self._notes)
        if not deduped:
            notes.append(f"no record matches {stripped!r} in any tier ({', '.join(TIERS)})")
        elif len(deduped) > 1:
            notes.append(
                f"{len(deduped)} records match at the {tier} tier — ambiguity is "
                "surfaced for review, never resolved silently"
            )
        return MatchResult(
            query=query,
            tier=tier,
            candidates=tuple(deduped),
            excluded=tuple(self._excluded),
            notes=tuple(notes),
        )


def facts_of(record: object) -> RecordFacts:
    """Adapt a CatalogRecord row (or anything record-shaped)."""
    return RecordFacts(
        record_id=record.id,  # type: ignore[attr-defined]
        source_id=record.source_id,  # type: ignore[attr-defined]
        display_name=record.display_name,  # type: ignore[attr-defined]
        aliases=tuple(record.aliases),  # type: ignore[attr-defined]
        effective_from=record.effective_from,  # type: ignore[attr-defined]
        effective_to=record.effective_to,  # type: ignore[attr-defined]
    )


async def build_match_index(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    binding: CatalogBinding,
    as_of: date | None = None,
) -> CatalogMatchIndex | None:
    """The index for a stream's bound catalog — tenant scope via the
    scoped repository, version scope via the CAT-001 binding. None when
    the binding resolves to no usable version."""
    version = await resolve_catalog_version(session, context, binding=binding)
    if version is None:
        return None
    records = await CatalogRecordRepository(session, context).list_for_version(version.id)
    return CatalogMatchIndex([facts_of(record) for record in records], as_of=as_of)


__all__ = [
    "TIERS",
    "CatalogMatchIndex",
    "MatchCandidate",
    "MatchResult",
    "RecordFacts",
    "build_match_index",
    "facts_of",
    "normalize_identifier",
    "normalize_text",
]
