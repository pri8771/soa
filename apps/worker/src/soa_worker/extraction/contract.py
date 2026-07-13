"""Reusable extraction-provider contract suite (PRC-006).

Every adapter's test module subclasses ``ExtractionProviderContract``
and implements ``make_provider`` plus ``contract_request`` (a request
the adapter can meaningfully extract from). The suite encodes the
behaviours downstream stages rely on — an adapter that cannot pass it
is not an ExtractionProvider, however good its vendor benchmarks.

Determinism note: the suite calls the provider twice through two
independently constructed instances and requires identical results.
Hosted adapters whose models genuinely drift must pin whatever the
vendor offers (seeds, temperature, model snapshot) to pass — that is
the point, not an inconvenience.
"""

from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionRequest,
    requested_keys,
    validate_result_against_request,
)


class ExtractionProviderContract:
    """Subclass per adapter; override make_provider() and contract_request()."""

    async def make_provider(self) -> ExtractionProvider:
        raise NotImplementedError

    def contract_request(self) -> ExtractionRequest:
        """A request this adapter can extract at least one value from."""
        raise NotImplementedError

    async def test_is_deterministic_across_independent_runs(self) -> None:
        request = self.contract_request()
        first = await (await self.make_provider()).extract(request)
        second = await (await self.make_provider()).extract(request)
        assert first == second, "two runs over the same request must be identical"

    async def test_extracts_at_least_one_value_from_the_contract_fixture(self) -> None:
        provider = await self.make_provider()
        result = await provider.extract(self.contract_request())
        assert any(f.raw_value is not None for f in result.fields), (
            "the contract fixture must yield at least one extracted value — "
            "an all-absent result proves nothing about the adapter"
        )

    async def test_result_conforms_to_the_request(self) -> None:
        request = self.contract_request()
        provider = await self.make_provider()
        result = await provider.extract(request)
        assert validate_result_against_request(request, result) == []

    async def test_result_names_the_provider(self) -> None:
        provider = await self.make_provider()
        result = await provider.extract(self.contract_request())
        assert result.provider == provider.name

    async def test_only_requested_fields_are_returned(self) -> None:
        request = self.contract_request()
        provider = await self.make_provider()
        result = await provider.extract(request)
        keys = requested_keys(request)
        assert all(f.field_key in keys for f in result.fields)

    async def test_absent_values_carry_no_evidence(self) -> None:
        provider = await self.make_provider()
        result = await provider.extract(self.contract_request())
        for extracted in result.fields:
            if extracted.raw_value is None:
                assert extracted.evidence == (), (
                    f"absent field {extracted.field_key!r} must not fabricate evidence"
                )

    async def test_evidence_stays_inside_page_bounds(self) -> None:
        request = self.contract_request()
        provider = await self.make_provider()
        result = await provider.extract(request)
        pages = {page.page_number: page for page in request.pages}
        for extracted in result.fields:
            for span in extracted.evidence:
                page = pages[span.page_number]
                for x, y in span.polygon:
                    assert 0 <= x <= page.width_px
                    assert 0 <= y <= page.height_px
