"""Evaluation runner tests (AIO-016): known-score fixtures with
hand-computed rates, exact vs normalized comparison, line metrics,
latency/cost/error accounting, resumability, repeatability, and the
no-external-calls-by-default guard."""

import pytest

from soa_worker.evaluation import (
    EvalDocument,
    EvalPrediction,
    EvaluationRefusedError,
    EvaluationState,
    run_evaluation,
    score_document,
)
from soa_worker.providers.capabilities import (
    ANY_LANGUAGE,
    LOCAL_DATA_POLICY,
    Capability,
    DataPolicy,
    ProviderInfo,
)

DOC_ONE = EvalDocument(
    document_sha256="a" * 64,
    split="test",
    ground_truth={
        "fields": {"po_number": "PO-4711", "currency": "EUR", "ship_date": None},
        "lines": [{"sku": "WIDGET-9", "quantity": "5"}],
    },
    expected_class="purchase_order",
)
DOC_TWO = EvalDocument(
    document_sha256="b" * 64,
    split="validation",
    ground_truth={"fields": {"po_number": "PO-9000", "total": "62.50"}},
)

#: doc one: po exact, currency case-differs (normalized only), ship_date
#: absent-matches-absent (exact); line cell sku exact, quantity wrong.
#: doc two: po wrong, total "62.5" (normalized numeric equivalence).
PREDICTIONS = {
    DOC_ONE.document_sha256: EvalPrediction(
        fields={"po_number": "PO-4711", "currency": "eur", "ship_date": None},
        lines=({"sku": "WIDGET-9", "quantity": "7"},),
        predicted_class="purchase_order",
        cost_cents=3,
    ),
    DOC_TWO.document_sha256: EvalPrediction(
        fields={"po_number": "PO-9999", "total": "62.5"},
        cost_cents=5,
    ),
}


async def scripted_extract(document: EvalDocument) -> EvalPrediction:
    return PREDICTIONS[document.document_sha256]


class TestKnownScores:
    async def test_the_hand_computed_rates_come_out_exactly(self) -> None:
        report, _ = await run_evaluation([DOC_ONE, DOC_TWO], scripted_extract)
        # Fields: 5 total. Exact: po1, ship_date(None==None) = 2. 2/5 = 0.4
        assert report.field_exact_rate == 0.4
        # Normalized adds currency (case) and total (62.5 == 62.50): 4/5.
        assert report.field_normalized_rate == 0.8
        # Line cells: 2 total, 1 exact.
        assert report.line_cell_exact_rate == 0.5
        # Only doc one declares an expected class, and it matched.
        assert report.class_accuracy == 1.0
        assert report.total_cost_cents == 8
        assert report.documents_scored == 2
        assert report.documents_failed == 0
        assert report.by_split == {"test": 1, "validation": 1}
        assert report.by_field["po_number"].total == 2
        assert report.by_field["po_number"].exact == 1

    async def test_latency_is_measured_with_the_injected_clock(self) -> None:
        ticks = iter([1.0, 1.25, 2.0, 2.5])  # 250ms and 500ms
        report, _ = await run_evaluation(
            [DOC_ONE, DOC_TWO], scripted_extract, clock=lambda: next(ticks)
        )
        assert report.mean_latency_ms == 375.0


class TestScoringSemantics:
    def test_absent_matches_absent_but_never_a_value(self) -> None:
        document = EvalDocument(
            document_sha256="c" * 64,
            split="test",
            ground_truth={"fields": {"a": None, "b": "x"}},
        )
        score, _ = score_document(document, EvalPrediction(fields={"a": "fabricated", "b": None}))
        assert score.fields_exact == 0

    def test_missing_predicted_rows_score_zero_cells(self) -> None:
        document = EvalDocument(
            document_sha256="d" * 64,
            split="test",
            ground_truth={"fields": {"a": "x"}, "lines": [{"sku": "S1"}, {"sku": "S2"}]},
        )
        score, _ = score_document(
            document, EvalPrediction(fields={"a": "x"}, lines=({"sku": "S1"},))
        )
        assert score.line_cells_total == 2
        assert score.line_cells_exact == 1
        assert score.lines_expected == 2
        assert score.lines_predicted == 1


class TestErrorsAndResume:
    async def test_a_failing_document_is_recorded_and_the_run_continues(self) -> None:
        async def flaky(document: EvalDocument) -> EvalPrediction:
            if document.document_sha256 == DOC_ONE.document_sha256:
                raise RuntimeError("provider exploded")
            return PREDICTIONS[document.document_sha256]

        report, state = await run_evaluation([DOC_ONE, DOC_TWO], flaky)
        assert report.documents_scored == 1
        assert report.documents_failed == 1
        assert state.errors[DOC_ONE.document_sha256] == "provider exploded"

    async def test_resume_never_pays_for_scored_documents_again(self) -> None:
        calls: list[str] = []

        async def counting(document: EvalDocument) -> EvalPrediction:
            calls.append(document.document_sha256)
            return PREDICTIONS[document.document_sha256]

        _, state = await run_evaluation([DOC_ONE], counting)
        assert calls == [DOC_ONE.document_sha256]
        # Round-trip the checkpoint like a real resume would.
        restored = EvaluationState.from_dict(state.to_dict())
        report, _ = await run_evaluation([DOC_ONE, DOC_TWO], counting, state=restored)
        assert calls == [DOC_ONE.document_sha256, DOC_TWO.document_sha256]  # doc one skipped
        assert report.documents_scored == 2

    async def test_the_report_is_repeatable(self) -> None:
        first, _ = await run_evaluation([DOC_TWO, DOC_ONE], scripted_extract)
        second, _ = await run_evaluation([DOC_ONE, DOC_TWO], scripted_extract)
        assert first.to_json() == second.to_json()


class TestExternalCallGuard:
    HOSTED = ProviderInfo(
        name="hosted-extractor",
        capability=Capability.FIELD_EXTRACTION,
        languages=(ANY_LANGUAGE,),
        data_policy=DataPolicy(
            processing_region="us",
            sends_content_to_third_party=True,
            retains_content=False,
            uses_content_for_training=False,
        ),
    )
    LOCAL = ProviderInfo(
        name="local-extractor",
        capability=Capability.FIELD_EXTRACTION,
        languages=(ANY_LANGUAGE,),
        data_policy=LOCAL_DATA_POLICY,
    )

    async def test_external_providers_are_refused_by_default(self) -> None:
        with pytest.raises(EvaluationRefusedError, match="allow_external"):
            await run_evaluation([DOC_ONE], scripted_extract, provider_info=self.HOSTED)

    async def test_external_providers_run_only_with_the_explicit_flag(self) -> None:
        report, _ = await run_evaluation(
            [DOC_ONE], scripted_extract, provider_info=self.HOSTED, allow_external=True
        )
        assert report.documents_scored == 1

    async def test_local_providers_need_no_flag(self) -> None:
        report, _ = await run_evaluation([DOC_ONE], scripted_extract, provider_info=self.LOCAL)
        assert report.documents_scored == 1
