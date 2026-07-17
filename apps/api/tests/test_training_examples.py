"""Few-shot compilation transforms (Phase 2): text excerpts, URL scrubbing,
and turning labelled gold documents into bounded exemplars."""

import uuid
from types import SimpleNamespace
from typing import Any

from soa_api.services.training_examples import (
    build_examples,
    document_text_excerpt,
    strip_urls,
)


def _gold(ground_truth: dict[str, Any], source_id: uuid.UUID | None) -> Any:
    return SimpleNamespace(ground_truth=ground_truth, source_document_id=source_id)


def test_strip_urls_removes_exfiltration_channels() -> None:
    assert strip_urls("see https://evil.example/x and http://a.b now") == "see  and  now"
    assert strip_urls("no urls here") == "no urls here"


def test_document_text_excerpt_joins_spans_bounded_and_scrubbed() -> None:
    geometry = {
        "pages": [
            {"spans": [{"text": "PURCHASE"}, {"text": "ORDER"}, {"text": "https://x.io"}]},
            {"spans": [{"text": "PO-1"}]},
        ]
    }
    assert document_text_excerpt(geometry, 100) == "PURCHASE ORDER  PO-1"
    assert document_text_excerpt(geometry, 8) == "PURCHASE"
    assert document_text_excerpt({}, 100) == ""


def test_build_examples_pairs_text_with_expected_values() -> None:
    doc_id = uuid.uuid4()
    gold = _gold(
        {
            "fields": {"po_number": "PO-1", "currency": "EUR", "ship_date": None},
            "lines": [{"sku": "A", "qty": "2"}, {"sku": None, "qty": None}],
        },
        doc_id,
    )
    examples = build_examples([gold], {doc_id: "PURCHASE ORDER PO-1"})
    assert examples == [
        {
            "fields": {"po_number": "PO-1", "currency": "EUR"},
            "text": "PURCHASE ORDER PO-1",
            "lines": [{"sku": "A", "qty": "2"}],
        }
    ]


def test_build_examples_drops_url_valued_fields() -> None:
    doc_id = uuid.uuid4()
    gold = _gold({"fields": {"po_number": "PO-1", "link": "http://evil.example"}}, doc_id)
    examples = build_examples([gold], {doc_id: ""})
    assert examples == [{"fields": {"po_number": "PO-1"}}]


def test_build_examples_skips_empty_and_bounds_count() -> None:
    empties = [_gold({"fields": {}}, None) for _ in range(3)]
    assert build_examples(empties, {}) == []
    many = [_gold({"fields": {"po_number": f"PO-{i}"}}, None) for i in range(20)]
    assert len(build_examples(many, {}, max_examples=4)) == 4


def test_strip_urls_removes_bare_scheme_fragments() -> None:
    # A line-wrapped URL leaves a bare scheme once the host lands elsewhere.
    assert strip_urls("see https:// then host.example later") == "see  then host.example later"
    assert strip_urls("trailing http://") == "trailing"


def test_build_examples_drops_url_in_key_and_bare_scheme_values() -> None:
    doc_id = uuid.uuid4()
    gold = _gold(
        {"fields": {"po_number": "PO-1", "http://x": "y", "link": "http://"}},
        doc_id,
    )
    examples = build_examples([gold], {doc_id: ""})
    assert examples == [{"fields": {"po_number": "PO-1"}}]


def test_build_examples_final_guard_drops_residual_scheme() -> None:
    # Even if a scheme sneaks through the text (excerpt already scrubbed here),
    # the compiled example must never carry a scheme the worker would refuse.
    doc_id = uuid.uuid4()
    gold = _gold({"fields": {"po_number": "PO-1"}}, doc_id)
    examples = build_examples([gold], {doc_id: "clean text"})
    import json as _json

    assert "http://" not in _json.dumps(examples).lower()
    assert "https://" not in _json.dumps(examples).lower()
