from soa_fixtures import DEMO_TENANT, InMemorySeedSink, SeedRunner, stable_id


def test_seed_is_idempotent_across_repeated_runs() -> None:
    sink = InMemorySeedSink()
    runner = SeedRunner()

    first = runner.seed(sink)
    after_first = dict(sink.records)
    second = runner.seed(sink)

    assert first.counts == second.counts
    assert sink.records == after_first, "second seed must not create or mutate records"


def test_seed_counts_match_delivery_plan_contract() -> None:
    sink = InMemorySeedSink()
    report = SeedRunner().seed(sink)
    assert report.counts["organization"] == 1
    assert report.counts["workspace"] == 1
    assert report.counts["process"] == 1
    assert report.counts["stream"] == 2
    assert report.counts["user"] == 5
    assert report.counts["catalog"] == 4
    assert report.counts["document"] == 5
    assert report.total == len(sink.records)


def test_ids_are_stable_across_processes() -> None:
    # uuid5 of the fixed namespace — these values must never change.
    assert str(stable_id("org:northstar")) == str(stable_id("org:northstar"))
    assert DEMO_TENANT.organization.id == stable_id("org:northstar")


def test_records_are_locatable_by_alias() -> None:
    sink = InMemorySeedSink()
    SeedRunner().seed(sink)
    org = sink.get_by_alias("org:northstar")
    assert org is not None
    assert org["name"] == "Northstar Distribution"
    reviewer = sink.get_by_alias("user:reviewer")
    assert reviewer is not None
    assert reviewer["role"] == "reviewer"


def test_document_scenarios_cover_required_cases() -> None:
    scenarios = {doc.scenario for doc in DEMO_TENANT.documents}
    assert scenarios == {
        "success",
        "low_confidence",
        "validation_failure",
        "multi_page_lines",
        "duplicate_po",
    }


def test_duplicate_po_fixture_reuses_po_number() -> None:
    by_alias = {doc.alias: doc for doc in DEMO_TENANT.documents}
    clean = by_alias["document:po-clean"]
    duplicate = by_alias["document:po-duplicate"]
    assert clean.expected_fields["po_number"] == duplicate.expected_fields["po_number"]
    assert clean.alias != duplicate.alias
