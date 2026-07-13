"""Customer/ship-to validation tests (CAT-011): the valid fixture is
clean, and each defect — wrong owner, wrong role, out-of-effect party,
country/postcode disagreement — fires its own finding with a written
message. Absent data degrades to warnings/notes, never silent passes."""

from datetime import date

from soa_db.catalog_validation import (
    DocumentAddress,
    OrderParties,
    PartyFacts,
    normalize_country,
    validate_parties,
)

ACME = PartyFacts(
    source_id="CUST-1",
    display_name="Acme GmbH",
    attributes={"roles": ["sold_to", "bill_to"], "country": "DE"},
)
ACME_HAMBURG = PartyFacts(
    source_id="ST-7",
    display_name="Acme GmbH Werk 2, Hamburg",
    attributes={
        "roles": ["ship_to"],
        "belongs_to": "CUST-1",
        "country": "DE",
        "postcode": "20095",
    },
)
RIVAL_DEPOT = PartyFacts(
    source_id="ST-9",
    display_name="Rival Logistics Depot",
    attributes={"roles": ["ship_to"], "belongs_to": "CUST-2", "country": "DE"},
)


class TestValidFixtures:
    def test_a_fully_consistent_order_has_no_findings(self) -> None:
        result = validate_parties(
            OrderParties(sold_to=ACME, bill_to=ACME, ship_to=ACME_HAMBURG),
            document_address=DocumentAddress(country="Germany", postcode="20095"),
            as_of=date(2026, 7, 13),
        )
        assert result.findings == ()
        assert any("verified" in note for note in result.notes)

    def test_records_without_roles_allow_every_role_with_a_note(self) -> None:
        undeclared = PartyFacts(source_id="CUST-3", display_name="Open Corp")
        result = validate_parties(OrderParties(sold_to=undeclared))
        assert result.findings == ()
        assert any("declares no roles" in note for note in result.notes)

    def test_unresolved_parties_are_skipped_with_notes(self) -> None:
        result = validate_parties(OrderParties())
        assert result.findings == ()
        assert sum("skipped" in note for note in result.notes) == 3


class TestShipToOwnership:
    def test_a_foreign_ship_to_is_an_error(self) -> None:
        result = validate_parties(OrderParties(sold_to=ACME, ship_to=RIVAL_DEPOT))
        (finding,) = result.errors
        assert finding.code == "ship_to_not_linked_to_customer"
        assert "CUST-2" in finding.message and "CUST-1" in finding.message
        assert finding.field_key == "ship_to"

    def test_the_owner_comparison_normalizes_identifiers(self) -> None:
        sloppy = PartyFacts(
            source_id="ST-8",
            display_name="Acme Lager",
            attributes={"belongs_to": "cust-001"},
        )
        result = validate_parties(OrderParties(sold_to=ACME, ship_to=sloppy))
        assert result.errors == ()  # cust-001 -> CUST1 == CUST-1 normalized

    def test_a_missing_link_is_a_warning_not_a_pass(self) -> None:
        unlinked = PartyFacts(source_id="ST-0", display_name="Somewhere")
        result = validate_parties(OrderParties(sold_to=ACME, ship_to=unlinked))
        (finding,) = result.warnings
        assert finding.code == "ship_to_link_unverifiable"

    def test_a_link_without_a_sold_to_is_noted_not_checked(self) -> None:
        result = validate_parties(OrderParties(ship_to=ACME_HAMBURG))
        assert result.errors == ()
        assert any("not checked" in note for note in result.notes)


class TestRolesAndEffectiveDates:
    def test_a_ship_to_used_as_sold_to_is_an_error(self) -> None:
        result = validate_parties(OrderParties(sold_to=ACME_HAMBURG))
        (finding,) = result.errors
        assert finding.code == "party_role_invalid"
        assert "sold-to" in finding.message
        assert finding.field_key == "customer_name"

    def test_malformed_roles_fail_closed(self) -> None:
        broken = PartyFacts(
            source_id="CUST-9", display_name="Broken", attributes={"roles": "sold_to"}
        )
        result = validate_parties(OrderParties(sold_to=broken))
        assert [f.code for f in result.errors] == ["party_role_invalid"]

    def test_an_out_of_effect_party_is_an_error(self) -> None:
        retired = PartyFacts(
            source_id="CUST-4",
            display_name="Closed AG",
            attributes={"roles": ["sold_to"]},
            effective_to=date(2025, 12, 31),
        )
        result = validate_parties(OrderParties(sold_to=retired), as_of=date(2026, 7, 13))
        (finding,) = result.errors
        assert finding.code == "party_out_of_effect"
        assert "2026-07-13" in finding.message

    def test_without_an_order_date_effective_windows_are_not_enforced(self) -> None:
        retired = PartyFacts(
            source_id="CUST-4",
            display_name="Closed AG",
            effective_to=date(2025, 12, 31),
        )
        assert validate_parties(OrderParties(sold_to=retired)).errors == ()


class TestAddressConsistency:
    def test_country_names_and_codes_agree(self) -> None:
        assert normalize_country("Germany") == "DE"
        assert normalize_country(" de ") == "DE"
        assert normalize_country("Atlantis") == "ATLANTIS"

    def test_a_country_contradiction_is_an_error(self) -> None:
        result = validate_parties(
            OrderParties(ship_to=ACME_HAMBURG),
            document_address=DocumentAddress(country="France"),
        )
        assert [f.code for f in result.errors] == ["address_country_mismatch"]

    def test_a_postcode_contradiction_is_an_error(self) -> None:
        result = validate_parties(
            OrderParties(ship_to=ACME_HAMBURG),
            document_address=DocumentAddress(country="DE", postcode="80331"),
        )
        assert [f.code for f in result.errors] == ["address_postcode_mismatch"]

    def test_postcode_spacing_and_case_do_not_false_alarm(self) -> None:
        dutch = PartyFacts(
            source_id="ST-NL",
            display_name="Rotterdam DC",
            attributes={"belongs_to": "CUST-NL", "country": "NL", "postcode": "3011 AB"},
        )
        result = validate_parties(
            OrderParties(ship_to=dutch),
            document_address=DocumentAddress(country="Netherlands", postcode="3011ab"),
        )
        assert result.findings == ()

    def test_a_postcode_that_cannot_fit_the_country_is_a_warning(self) -> None:
        odd = PartyFacts(
            source_id="ST-DE",
            display_name="Berlin DC",
            attributes={"belongs_to": "CUST-1", "country": "DE", "postcode": "ABC-12"},
        )
        result = validate_parties(
            OrderParties(ship_to=odd),
            document_address=DocumentAddress(country="DE", postcode="ABC-12"),
        )
        (finding,) = result.warnings
        assert finding.code == "postcode_format_invalid"

    def test_unknown_country_formats_are_skipped_with_a_note(self) -> None:
        exotic = PartyFacts(
            source_id="ST-XX",
            display_name="Elsewhere",
            attributes={"belongs_to": "CUST-1", "country": "XX", "postcode": "??!!"},
        )
        result = validate_parties(
            OrderParties(ship_to=exotic),
            document_address=DocumentAddress(country="XX", postcode="??!!"),
        )
        assert result.findings == ()
        assert any("no postcode format is known" in note.lower() for note in result.notes)

    def test_a_record_without_a_country_is_noted_not_guessed(self) -> None:
        bare = PartyFacts(source_id="ST-BARE", display_name="No address facts")
        result = validate_parties(
            OrderParties(ship_to=bare),
            document_address=DocumentAddress(country="DE", postcode="20095"),
        )
        assert [f.code for f in result.findings] == ["ship_to_link_unverifiable"]
        assert any("was not cross-checked" in note for note in result.notes)


class TestReasonShape:
    def test_findings_serialize_as_route_reasons(self) -> None:
        result = validate_parties(OrderParties(sold_to=ACME, ship_to=RIVAL_DEPOT))
        reason = result.errors[0].to_reason()
        assert reason["code"] == "ship_to_not_linked_to_customer"
        assert reason["rule_key"] == "catalog.ship_to_not_linked_to_customer"
        assert reason["field_key"] == "ship_to"
        assert reason["message"]
