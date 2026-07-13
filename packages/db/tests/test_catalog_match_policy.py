"""Match policy tests (CAT-009): outcome routing by field type,
ambiguity guards, the serializable retention record with feature
scores, and reviewer overrides as labeled evaluation examples."""

import uuid

import pytest

from soa_db.catalog_fuzzy import FuzzyConfig
from soa_db.catalog_match_policy import (
    FieldMatchPolicy,
    MatchPolicyError,
    MatchPolicySet,
    override_to_evaluation_example,
    resolve_match,
)
from soa_db.catalog_matching import RecordFacts


def rec(source_id: str, name: str, aliases: tuple[str, ...] = ()) -> RecordFacts:
    return RecordFacts(
        record_id=uuid.uuid5(uuid.NAMESPACE_URL, source_id),
        source_id=source_id,
        display_name=name,
        aliases=aliases,
    )


MATERIALS = [
    rec("SKU-9", "Widget 9mm", ("WIDGET-9",)),
    rec("SKU-10", "Widget 12mm"),
    rec("SKU-11", "Flange Kit"),
]


class TestOutcomes:
    def test_an_unambiguous_exact_hit_auto_matches(self) -> None:
        decision = resolve_match("SKU-9", MATERIALS, field_type="material")
        assert decision.outcome == "auto_matched"
        assert decision.selected_source_id == "SKU-9"
        assert decision.tier == "exact_identifier"
        assert any("auto-matched at the exact_identifier tier" in r for r in decision.reasons)

    def test_exact_tier_ambiguity_never_auto_matches(self) -> None:
        twins = [rec("A-1", "Same Name"), rec("A-2", "Same Name")]
        decision = resolve_match("Same Name", twins, field_type="generic")
        assert decision.outcome == "needs_review"
        assert decision.selected_source_id is None
        assert len(decision.exact_candidates) == 2
        assert any("ambiguity never auto-matches" in r for r in decision.reasons)

    def test_a_confident_fuzzy_lead_auto_matches(self) -> None:
        policy_set = MatchPolicySet(
            policies={
                "material": FieldMatchPolicy(
                    auto_match_min=0.75,
                    min_lead=0.05,
                    fuzzy=FuzzyConfig(
                        weights={"text_trigram": 0.5, "text_sequence": 0.5},
                        accept_threshold=0.75,
                        suggest_threshold=0.3,
                    ),
                )
            }
        )
        decision = resolve_match(
            "Widgets 9mm", MATERIALS, field_type="material", policy_set=policy_set
        )
        assert decision.outcome == "auto_matched"
        assert decision.selected_source_id == "SKU-9"
        assert any("fuzzy auto-match" in r for r in decision.reasons)

    def test_a_narrow_lead_routes_to_review(self) -> None:
        near_twins = [rec("SKU-A", "Widget Type A"), rec("SKU-B", "Widget Type B")]
        policy_set = MatchPolicySet(
            policies={
                "material": FieldMatchPolicy(
                    auto_match_min=0.5,
                    min_lead=0.2,
                    fuzzy=FuzzyConfig(
                        weights={"text_sequence": 1.0},
                        accept_threshold=0.5,
                        suggest_threshold=0.3,
                    ),
                )
            }
        )
        decision = resolve_match(
            "Widget Type", near_twins, field_type="material", policy_set=policy_set
        )
        assert decision.outcome == "needs_review"
        assert any("too close to auto-match" in r for r in decision.reasons)

    def test_nothing_plausible_is_an_explicit_no_match(self) -> None:
        decision = resolve_match("Unrelated Chemicals", MATERIALS, field_type="material")
        assert decision.outcome == "no_match"
        assert decision.selected_source_id is None
        assert any("explicit no-match" in r for r in decision.reasons)

    def test_unknown_field_types_fail_closed(self) -> None:
        with pytest.raises(MatchPolicyError, match="no match policy"):
            resolve_match("x", MATERIALS, field_type="star_sign")

    def test_contradictory_policies_are_refused(self) -> None:
        with pytest.raises(MatchPolicyError, match="auto-match candidates"):
            FieldMatchPolicy(auto_match_min=0.5, fuzzy=FuzzyConfig(accept_threshold=0.9))


class TestRetention:
    def test_the_record_retains_selection_and_every_feature_score(self) -> None:
        policy_set = MatchPolicySet(
            policies={
                "material": FieldMatchPolicy(
                    auto_match_min=0.75,
                    fuzzy=FuzzyConfig(
                        weights={"text_trigram": 0.5, "text_sequence": 0.5},
                        accept_threshold=0.75,
                        suggest_threshold=0.3,
                    ),
                )
            }
        )
        decision = resolve_match(
            "Widgets 9mm", MATERIALS, field_type="material", policy_set=policy_set
        )
        record = decision.to_record()
        assert record["selected_source_id"] == "SKU-9"
        assert record["config_fingerprint"] == policy_set.policies["material"].fuzzy.fingerprint
        top = record["fuzzy_candidates"][0]
        assert top["source_id"] == "SKU-9"
        assert {f["feature"] for f in top["features"]} == {"text_trigram", "text_sequence"}
        assert all(f["detail"] for f in top["features"])
        assert record["reasons"]

    def test_exact_decisions_retain_their_tier_and_reason(self) -> None:
        record = resolve_match("SKU-9", MATERIALS, field_type="material").to_record()
        assert record["tier"] == "exact_identifier"
        assert record["exact_candidates"][0]["reason"]


class TestOverrides:
    def decision(self):
        return resolve_match("Widget 9m", MATERIALS, field_type="material")

    def test_an_override_becomes_a_labeled_example(self) -> None:
        decision = self.decision()
        example = override_to_evaluation_example(
            decision,
            chosen_source_id="SKU-10",
            actor_id="user:reviewer",
            reason="the buyer's 9m is the 12mm bulk variant",
        )
        assert example["kind"] == "catalog_match_override"
        assert example["human_selected_source_id"] == "SKU-10"
        assert example["machine_outcome"] == decision.outcome
        assert example["agreed"] == (decision.selected_source_id == "SKU-10")
        assert "SKU-10" not in example["rejected_candidates"]
        assert example["config_fingerprint"] == decision.config_fingerprint

    def test_confirming_no_match_is_a_valid_label(self) -> None:
        example = override_to_evaluation_example(
            self.decision(),
            chosen_source_id=None,
            actor_id="user:reviewer",
            reason="free-text item; not in the catalog",
        )
        assert example["human_selected_source_id"] is None

    def test_an_override_needs_a_written_reason(self) -> None:
        with pytest.raises(MatchPolicyError, match="written reason"):
            override_to_evaluation_example(
                self.decision(), chosen_source_id="SKU-9", actor_id="user:x", reason="  "
            )
