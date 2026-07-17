"""Compile a training set's labelled samples into few-shot exemplars.

The labelled samples in a published training set (Phase 1) become the versioned
``examples`` slot on a stream's extraction instructions (Phase 2). Each exemplar
pairs a bounded excerpt of the sample's real document text with its expected
``fields``/``lines`` — an input→output pair the model learns the mapping from,
rather than a bag of output values it might parrot.

Everything here is a pure transform over already-loaded data so it is easy to
test and deterministic: same published set → same exemplars → same prompt.
"""

import json
import re
from typing import Any

from soa_db.gold_datasets import GoldDocument
from soa_db.instructions import (
    MAX_EXAMPLE_TEXT_CHARS,
    MAX_EXAMPLE_VALUE_CHARS,
    MAX_EXAMPLES,
)

#: A stream may have no authored instructions yet; few-shot still needs a
#: non-empty preamble to attach to (the content shape requires one).
DEFAULT_INSTRUCTIONS = (
    "Extract the requested fields from this business document, copying values "
    "verbatim from the text."
)

#: ``\S*`` (not ``\S+``) so a *bare* scheme fragment like ``https://`` — which
#: a line-wrapped URL leaves behind once the host lands on the next span — is
#: also removed. The worker refuses ANY ``http://``/``https://`` substring in
#: instruction content, so scrubbing must be at least as aggressive or a
#: surviving fragment breaks extraction for the whole stream.
_URL = re.compile(r"https?://\S*", re.IGNORECASE)
_SCHEMES = ("http://", "https://")


def strip_urls(text: str) -> str:
    """Remove URLs — a URL inside prompt configuration is an exfiltration
    channel the request builder refuses outright, so example text is scrubbed
    before it can ever reach the model."""
    return _URL.sub("", text).strip()


def _url_free(value: str) -> bool:
    # The EXACT check the worker's request builder applies, so anything this
    # accepts is guaranteed not to trip _validate_instructions at extraction.
    lowered = value.lower()
    return all(scheme not in lowered for scheme in _SCHEMES)


def document_text_excerpt(geometry: dict[str, Any], max_chars: int) -> str:
    """A bounded, reading-order text excerpt from a document's positioned-text
    geometry (the ``text_geometry`` artifact), URL-stripped."""
    pages = geometry.get("pages")
    if not isinstance(pages, list):
        return ""
    words: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        spans = page.get("spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            text = span.get("text") if isinstance(span, dict) else None
            if isinstance(text, str) and text.strip():
                words.append(text.strip())
    return strip_urls(" ".join(words))[:max_chars].strip()


def build_examples(
    gold_documents: list[GoldDocument],
    texts_by_document_id: dict[Any, str],
    *,
    max_examples: int = MAX_EXAMPLES,
    max_chars: int = MAX_EXAMPLE_TEXT_CHARS,
) -> list[dict[str, Any]]:
    """Turn labelled gold documents into bounded few-shot exemplars.

    ``texts_by_document_id`` maps a gold document's ``source_document_id`` to
    that document's text excerpt (empty string when unavailable — the exemplar
    still teaches expected value formats from its fields alone). Documents are
    taken in a stable order; only those with at least one expected value or
    some text contribute.
    """
    examples: list[dict[str, Any]] = []
    for gold in gold_documents:
        if len(examples) >= max_examples:
            break
        ground_truth = gold.ground_truth or {}
        raw_fields = ground_truth.get("fields", {})
        # A URL anywhere — a value OR a field key — would make the request
        # builder refuse the whole instruction content, so such entries are
        # dropped from the exemplar.
        fields = {
            str(key): value
            for key, value in raw_fields.items()
            if isinstance(value, str)
            and value.strip()
            and len(value) <= MAX_EXAMPLE_VALUE_CHARS
            and _url_free(value)
            and _url_free(str(key))
        }
        text = ""
        if gold.source_document_id is not None:
            text = strip_urls(str(texts_by_document_id.get(gold.source_document_id, "")))[
                :max_chars
            ].strip()
        if not fields and not text:
            continue
        example: dict[str, Any] = {"fields": fields}
        if text:
            example["text"] = text
        raw_lines = ground_truth.get("lines")
        if isinstance(raw_lines, list):
            rows = [
                {
                    str(k): v
                    for k, v in row.items()
                    if _url_free(str(k))
                    and (
                        v is None
                        or (
                            isinstance(v, str)
                            and len(v) <= MAX_EXAMPLE_VALUE_CHARS
                            and _url_free(v)
                        )
                    )
                }
                for row in raw_lines
                if isinstance(row, dict) and any(v for v in row.values())
            ]
            if rows:
                example["lines"] = rows
        # Final belt-and-suspenders guard: drop any exemplar that would still
        # trip the worker's substring URL check, so compiled examples can never
        # cause a stream-wide extraction outage.
        if not _url_free(json.dumps(example, ensure_ascii=False)):
            continue
        examples.append(example)
    return examples
