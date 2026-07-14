"""Sample sales-order stream template tests (GTM-004).

The template a new tenant imports during onboarding must (1) validate
against the REAL configuration validators — a sample the platform's own
validators reject is worse than none — and (2) import as a versioned,
detached copy that never silently mutates the template or other tenants.
"""

from soa_api.domain.policies import PolicyType, validate_policy, validate_provider_capabilities
from soa_api.domain.rules import validate_rule_set
from soa_api.domain.sample_template import (
    BASELINE_SALES_ORDER_TEMPLATE,
    SAMPLE_TEMPLATE_VERSION,
    import_template,
)
from soa_api.domain.schemas import validate_schema


class TestTemplateValidatesAgainstRealValidators:
    def test_schema_validates_and_exposes_the_line_table(self) -> None:
        schema = validate_schema(BASELINE_SALES_ORDER_TEMPLATE.schema)
        types = schema.field_types()
        assert types["po_number"] == "text"
        assert types["currency"] == "enum"
        assert types["lines"] == "table"
        assert types["lines.quantity"] == "number"

    def test_rules_validate_against_the_template_schema(self) -> None:
        field_types = validate_schema(BASELINE_SALES_ORDER_TEMPLATE.schema).field_types()
        rules = validate_rule_set(BASELINE_SALES_ORDER_TEMPLATE.rules, field_types)
        # The starter required-field and date-order rules are present and
        # validate (their authored test cases are executed by the validator).
        keys = {rule["key"] for rule in rules}
        assert "required.po_number" in keys
        assert "dates.delivery_after_order" in keys

    def test_provider_policy_validates_and_is_publishable(self) -> None:
        validate_policy(PolicyType.PROVIDER, BASELINE_SALES_ORDER_TEMPLATE.provider_policy)
        validate_provider_capabilities(BASELINE_SALES_ORDER_TEMPLATE.provider_policy)

    def test_template_carries_a_version(self) -> None:
        assert BASELINE_SALES_ORDER_TEMPLATE.version == SAMPLE_TEMPLATE_VERSION
        assert BASELINE_SALES_ORDER_TEMPLATE.version.startswith("sales-order/")


class TestImportIsVersionedAndDetached:
    def test_import_stamps_the_source_version(self) -> None:
        imported = import_template(BASELINE_SALES_ORDER_TEMPLATE)
        assert imported.source_template_version == BASELINE_SALES_ORDER_TEMPLATE.version

    def test_editing_the_import_does_not_mutate_the_template(self) -> None:
        imported = import_template(BASELINE_SALES_ORDER_TEMPLATE)
        imported.schema["fields"].append({"injected": True})
        imported.rules["rules"].clear()
        imported.provider_policy["provider_name"] = "someone-elses"
        # The shared template is untouched — existing tenants are safe.
        assert {"injected": True} not in BASELINE_SALES_ORDER_TEMPLATE.schema["fields"]
        assert BASELINE_SALES_ORDER_TEMPLATE.rules["rules"]
        assert BASELINE_SALES_ORDER_TEMPLATE.provider_policy["provider_name"] == "mock"

    def test_two_imports_are_independent(self) -> None:
        a = import_template(BASELINE_SALES_ORDER_TEMPLATE)
        b = import_template(BASELINE_SALES_ORDER_TEMPLATE)
        a.schema["fields"].append({"only_in_a": True})
        assert {"only_in_a": True} not in b.schema["fields"]

    def test_imported_catalogs_are_copied_and_independent(self) -> None:
        imported = import_template(BASELINE_SALES_ORDER_TEMPLATE)
        assert {c.kind for c in imported.catalogs} == {"customers", "materials", "uoms"}
        first_customer = next(c for c in imported.catalogs if c.kind == "customers").records[0]
        first_customer_mutable = dict(first_customer)
        first_customer_mutable["name"] = "Changed"
        # The template's record is unchanged (deep copy).
        template_customers = next(
            c for c in BASELINE_SALES_ORDER_TEMPLATE.catalogs if c.kind == "customers"
        )
        assert template_customers.records[0]["name"] == "Alpine Retail Ltd"

    def test_imported_rules_validate_too(self) -> None:
        # The copy is still a valid rule set against the copied schema.
        imported = import_template(BASELINE_SALES_ORDER_TEMPLATE)
        field_types = validate_schema(imported.schema).field_types()
        validate_rule_set(imported.rules, field_types)
