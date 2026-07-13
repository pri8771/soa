"""Backward-compatibility rules (CAN-001): newer schema versions may
only widen. The checker is exercised on synthetic narrowings, and every
shipped version pair is verified compatible."""

import copy
from itertools import pairwise
from typing import Any

from soa_canonical import breaking_changes, list_versions, load_schema


def base() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "nickname": {"type": ["string", "null"]},
            "status": {"enum": ["open", "closed"]},
            "tags": {"type": "array", "minItems": 0, "items": {"type": "string"}},
            "child": {
                "type": "object",
                "required": [],
                "properties": {"depth": {"type": "integer", "minimum": 0}},
            },
        },
        "$defs": {"money": {"type": "object", "properties": {"amount": {"type": "string"}}}},
    }


def test_pure_widening_is_compatible() -> None:
    old = base()
    new = copy.deepcopy(old)
    new["properties"]["brand_new_optional"] = {"type": ["string", "null"]}
    new["properties"]["status"]["enum"] = ["open", "closed", "archived"]
    new["$defs"]["money"]["properties"]["currency"] = {"type": "string"}
    assert breaking_changes(old, new) == []


def test_narrowings_are_flagged() -> None:
    old = base()

    removed = copy.deepcopy(old)
    del removed["properties"]["nickname"]
    assert any("nickname: property removed" in p for p in breaking_changes(old, removed))

    newly_required = copy.deepcopy(old)
    newly_required["required"] = ["name", "nickname"]
    assert any("'nickname' became required" in p for p in breaking_changes(old, newly_required))

    retyped = copy.deepcopy(old)
    retyped["properties"]["nickname"] = {"type": "string"}  # null branch dropped
    assert any("type narrowed" in p for p in breaking_changes(old, retyped))

    narrowed_enum = copy.deepcopy(old)
    narrowed_enum["properties"]["status"]["enum"] = ["open"]
    assert any("enum values removed" in p for p in breaking_changes(old, narrowed_enum))

    raised_bound = copy.deepcopy(old)
    raised_bound["properties"]["tags"]["minItems"] = 1
    assert any("minItems raised" in p for p in breaking_changes(old, raised_bound))

    nested = copy.deepcopy(old)
    nested["properties"]["child"]["required"] = ["depth"]
    assert any("'depth' became required" in p for p in breaking_changes(old, nested))

    dropped_def = copy.deepcopy(old)
    del dropped_def["$defs"]["money"]
    assert any("definition removed" in p for p in breaking_changes(old, dropped_def))


def test_every_shipped_version_pair_is_backward_compatible() -> None:
    """The rule that makes 'versioned' mean something: version N+1 must
    accept every payload version N accepted. With one shipped version
    this is vacuous — the pairwise walk keeps it enforced from the
    moment version two lands."""
    versions = list_versions()
    assert versions, "at least one canonical schema version must ship"
    for older, newer in pairwise(versions):
        problems = breaking_changes(load_schema(older), load_schema(newer))
        assert problems == [], f"{older} -> {newer} breaks compatibility: {problems}"
