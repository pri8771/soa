"""Promotion gate tests (AIO-017): per-field and per-cohort diffs,
critical-regression and false-auto-approval prohibitions, and waiver
semantics (reason + audit required; prohibited findings unbypassable)."""

import pytest

from soa_worker.evaluation import (
    EvalDocument,
    EvalPrediction,
    EvaluationState,
    build_report,
    score_document,
)
from soa_worker.evaluation_gate import (
    GatePolicy,
    PromotionBlockedError,
    Waiver,
    assert_promotable,
    compare_reports,
)

DOCS = [
    EvalDocument(
        document_sha256=f"{index:064x}",
        split="test" if index % 2 else "validation",
        ground_truth={"fields": {"po_number": f"PO-{index}", "total": "10.00"}},
    )
    for index in range(4)
]


def report_for(correct_po: int, correct_total: int, *, false_approvals: int = 0):
    """Build a real report where the first N documents get each field
    right, and the first ``false_approvals`` wrong documents would have
    been auto-approved."""
    state = EvaluationState()
    by_field: dict = {}
    approvals_left = false_approvals
    for index, document in enumerate(DOCS):
        po_right = index < correct_po
        total_right = index < correct_total
        wrong = not (po_right and total_right)
        auto = False
        if wrong and approvals_left > 0:
            auto = True
            approvals_left -= 1
        prediction = EvalPrediction(
            fields={
                "po_number": document.ground_truth["fields"]["po_number"] if po_right else "WRONG",
                "total": "10.00" if total_right else "99.99",
            },
            would_auto_approve=auto,
        )
        score, contribution = score_document(document, prediction)
        state.scores[document.document_sha256] = score
        for key, part in contribution.items():
            existing = by_field.get(key)
            if existing is None:
                by_field[key] = part
            else:
                from soa_worker.evaluation import FieldScore

                by_field[key] = FieldScore(
                    total=existing.total + part.total,
                    exact=existing.exact + part.exact,
                    normalized=existing.normalized + part.normalized,
                )
    return build_report(state, DOCS, by_field)


class TestComparison:
    def test_an_equal_candidate_passes(self) -> None:
        result = compare_reports(report_for(4, 4), report_for(4, 4))
        assert result.passed is True
        assert result.findings == ()

    def test_field_diffs_name_the_specific_field(self) -> None:
        result = compare_reports(report_for(4, 4), report_for(2, 4))
        by_field = {diff.field: diff for diff in result.field_diffs}
        assert by_field["po_number"].delta == -0.5
        assert by_field["total"].delta == 0.0
        (finding,) = [f for f in result.findings if f.kind == "field_regression"]
        assert "po_number" in finding.detail
        assert finding.waivable is True

    def test_improvements_are_not_findings(self) -> None:
        result = compare_reports(report_for(2, 4), report_for(4, 4))
        assert result.passed is True

    def test_cohort_regressions_are_found_even_when_the_average_hides_them(self) -> None:
        # Candidate: po_number right only on validation-split documents
        # (indices 0,2) — the "test" cohort collapses while the overall
        # average only halves.
        current = report_for(4, 4)
        candidate = report_for(2, 4)
        result = compare_reports(current, candidate)
        cohorts = {diff.cohort: diff for diff in result.cohort_diffs}
        assert cohorts["test"].delta is not None and cohorts["test"].delta < 0
        assert any(finding.kind == "cohort_regression" for finding in result.findings)


class TestProhibitions:
    def test_any_critical_field_regression_is_prohibited(self) -> None:
        policy = GatePolicy(max_field_regression=0.9, critical_fields=("po_number",))
        result = compare_reports(report_for(4, 4), report_for(3, 4), policy)
        (finding,) = [f for f in result.findings if f.kind == "critical_field_regression"]
        assert finding.waivable is False
        with pytest.raises(PromotionBlockedError, match="cannot be waived"):
            assert_promotable(result, waiver=Waiver(actor_id="user:boss", reason="ship it"))

    def test_a_rising_false_auto_approval_rate_is_prohibited(self) -> None:
        current = report_for(4, 4)
        candidate = report_for(2, 4, false_approvals=2)
        result = compare_reports(current, candidate)
        finding = next(f for f in result.findings if f.kind == "false_auto_approval")
        assert finding.waivable is False
        assert candidate.false_auto_approval_rate == 0.5
        with pytest.raises(PromotionBlockedError, match="false-auto-approval"):
            assert_promotable(result, waiver=Waiver(actor_id="user:boss", reason="ship it"))


class TestWaivers:
    def result_with_waivable_finding(self):
        return compare_reports(report_for(4, 4), report_for(3, 4))

    def test_findings_block_publication_without_a_waiver(self) -> None:
        with pytest.raises(PromotionBlockedError, match="waiver with a written reason"):
            assert_promotable(self.result_with_waivable_finding())

    def test_a_waiver_needs_a_written_reason(self) -> None:
        with pytest.raises(ValueError, match="written reason"):
            Waiver(actor_id="user:boss", reason="   ")

    def test_a_valid_waiver_passes_and_is_audited(self) -> None:
        audited: list[dict] = []
        assert_promotable(
            self.result_with_waivable_finding(),
            waiver=Waiver(actor_id="user:boss", reason="cosmetic field, pilot deadline"),
            audit_sink=audited.append,
        )
        (record,) = audited
        assert record["action"] == "evaluation.gate_waived"
        assert record["actor_id"] == "user:boss"
        assert "cosmetic field" in record["reason"]
        assert record["findings"]

    def test_a_clean_result_needs_no_waiver_and_no_audit(self) -> None:
        audited: list[dict] = []
        assert_promotable(
            compare_reports(report_for(4, 4), report_for(4, 4)), audit_sink=audited.append
        )
        assert audited == []
