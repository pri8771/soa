"""Weighted fuzzy matching tests (CAT-007): labeled customer/material/
ship-to fixtures, per-feature explanations that reconstruct the
aggregate, thresholds and verdicts, the candidate cap, and the pinned
configuration fingerprint."""

import uuid

import pytest

from soa_db.catalog_fuzzy import FuzzyConfig, FuzzyConfigError, fuzzy_match
from soa_db.catalog_matching import RecordFacts


def rec(source_id: str, name: str, aliases: tuple[str, ...] = ()) -> RecordFacts:
    return RecordFacts(
        record_id=uuid.uuid5(uuid.NAMESPACE_URL, source_id),
        source_id=source_id,
        display_name=name,
        aliases=aliases,
    )


CUSTOMERS = [
    rec("CUST-001", "Acme GmbH", ("ACME",)),
    rec("CUST-002", "Ajax Industries Ltd"),
    rec("CUST-003", "Meridian Foods AG"),
]

MATERIALS = [
    rec("SKU-9", "Widget 9mm", ("WIDGET-9",)),
    rec("SKU-10", "Widget 12mm"),
    rec("SKU-11", "Flange Kit"),
]

SHIP_TOS = [
    rec("ST-1", "Acme GmbH Werk 2, Hamburg", ("Acme Hamburg Warehouse",)),
    rec("ST-2", "Acme GmbH Zentrale, Berlin"),
]


class TestLabeledFixtures:
    def test_customer_with_extra_tokens_still_tops(self) -> None:
        result = fuzzy_match("Acme GmbH Werk", CUSTOMERS)
        assert result.candidates[0].source_id == "CUST-001"
        assert result.candidates[0].verdict in ("accept", "suggest")

    def test_material_typo_ranks_the_right_sku_first(self) -> None:
        result = fuzzy_match("Wdget 9mm", MATERIALS)
        assert result.candidates[0].source_id == "SKU-9"
        assert result.candidates[0].total_score < 1.0  # honest: not exact

    def test_ship_to_matches_via_the_alias(self) -> None:
        result = fuzzy_match("ACME Hamburg warehouse", SHIP_TOS)
        top = result.candidates[0]
        assert top.source_id == "ST-1"
        assert any("Acme Hamburg Warehouse" in f.detail for f in top.features)

    def test_unrelated_values_score_below_the_suggest_bar(self) -> None:
        result = fuzzy_match("Unrelated Chemicals Corp", MATERIALS)
        assert result.candidates == ()
        assert any("no record scored" in note for note in result.notes)


class TestExplainability:
    def test_the_aggregate_is_exactly_the_weighted_mean_of_its_features(self) -> None:
        config = FuzzyConfig()
        result = fuzzy_match("Widget 9mm", MATERIALS, config)
        weight_sum = sum(config.weights.values())
        for candidate in result.candidates:
            assert {f.feature for f in candidate.features} == set(config.weights)
            recomputed = sum(f.weight * f.score for f in candidate.features) / weight_sum
            assert candidate.total_score == pytest.approx(recomputed, abs=1e-3)
            for feature in candidate.features:
                assert feature.detail  # every score names what it compared

    def test_verdicts_follow_the_thresholds(self) -> None:
        # Text-only weighting: an exact-equal name scores a clean 1.0.
        config = FuzzyConfig(
            weights={"text_trigram": 0.5, "text_sequence": 0.5},
            accept_threshold=0.95,
            suggest_threshold=0.3,
        )
        result = fuzzy_match("Widget 9mm", MATERIALS, config)
        top = result.candidates[0]
        assert top.source_id == "SKU-9"
        assert top.total_score == 1.0
        assert top.verdict == "accept"
        weaker = [c for c in result.candidates if c.source_id == "SKU-10"]
        assert weaker and weaker[0].verdict == "suggest"


class TestBoundsAndDeterminism:
    def test_the_candidate_cap_drops_with_a_note(self) -> None:
        many = [rec(f"SKU-{n:02}", f"Widget {n}mm") for n in range(1, 11)]
        config = FuzzyConfig(candidate_cap=3, suggest_threshold=0.3)
        result = fuzzy_match("Widget 5mm", many, config)
        assert len(result.candidates) == 3
        assert any("dropped by the cap of 3" in note for note in result.notes)

    def test_ties_break_by_source_id_regardless_of_input_order(self) -> None:
        twins = [rec("SKU-B", "Same Widget"), rec("SKU-A", "Same Widget")]
        forward = fuzzy_match("Same Widget", twins)
        backward = fuzzy_match("Same Widget", list(reversed(twins)))
        assert [c.source_id for c in forward.candidates] == ["SKU-A", "SKU-B"]
        assert forward.candidates == backward.candidates


class TestPinnedConfiguration:
    def test_the_fingerprint_is_stable_and_travels_on_the_result(self) -> None:
        first = fuzzy_match("Widget 9mm", MATERIALS)
        second = fuzzy_match("anything else", MATERIALS)
        assert first.config_fingerprint == second.config_fingerprint
        assert first.accept_threshold == FuzzyConfig().accept_threshold

    def test_changing_the_configuration_changes_the_fingerprint(self) -> None:
        default = FuzzyConfig()
        reweighted = FuzzyConfig(
            weights={"identifier": 0.5, "text_trigram": 0.3, "text_sequence": 0.2}
        )
        assert default.fingerprint != reweighted.fingerprint

    def test_invalid_configurations_are_refused(self) -> None:
        with pytest.raises(FuzzyConfigError, match="unknown features"):
            FuzzyConfig(weights={"vibes": 1.0})
        with pytest.raises(FuzzyConfigError, match="contradiction"):
            FuzzyConfig(accept_threshold=0.5, suggest_threshold=0.8)
        with pytest.raises(FuzzyConfigError, match="negative"):
            FuzzyConfig(weights={"identifier": -1.0, "text_trigram": 2.0})
