"""Model generation tests (CAN-002): the generated Python/TypeScript
models are reproducible pure functions of the schema, the committed
files match the generator byte for byte (drift check), and the
generated Python model round-trips through schema validation."""

from pathlib import Path

from soa_canonical import validate_order
from soa_canonical.generate import (
    PY_TARGET,
    TS_TARGET,
    python_source,
    typescript_source,
)
from soa_canonical.models import CanonicalOrder, LineItem, Money

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_generation_is_reproducible() -> None:
    assert python_source() == python_source()
    assert typescript_source() == typescript_source()
    for source in (python_source(), typescript_source()):
        assert "GENERATED" in source
        assert "DO NOT EDIT" in source


def test_committed_python_model_matches_the_generator() -> None:
    committed = (REPO_ROOT / PY_TARGET).read_text("utf-8")
    assert committed == python_source(), (
        "models.py drifted from the schema — run: uv run python -m soa_canonical.generate"
    )


def test_committed_typescript_model_matches_the_generator() -> None:
    committed = (REPO_ROOT / TS_TARGET).read_text("utf-8")
    assert committed == typescript_source(), (
        "canonical-order.ts drifted from the schema — run: uv run python -m soa_canonical.generate"
    )


def test_generated_model_satisfies_the_schema() -> None:
    """A payload built through the generated TypedDicts validates — the
    models and the schema describe the same shape."""
    order: CanonicalOrder = {
        "schema_version": "1.0.0",
        "identifiers": {"po_number": "PO-1"},
        "dates": {"order_date": "2026-03-14"},
        "terms": {"currency": "USD"},
        "totals": {"grand_total": Money(amount="450.00", currency="USD")},
        "line_items": [
            LineItem(
                line_number=1,
                quantity="10",
                line_total=Money(amount="450.00", currency="USD"),
            )
        ],
        "source": {
            "document_id": "8a111111-1111-4111-8111-111111111111",
            "run_id": "a2222222-2222-4222-8222-222222222222",
        },
    }
    validate_order(dict(order))
