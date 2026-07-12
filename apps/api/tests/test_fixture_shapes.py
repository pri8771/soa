"""Demo fixtures must validate against the REAL configuration validators —
seed data that the shipped validators reject is worse than no seed data."""

from soa_api.domain.policies import PolicyType, validate_policy, validate_provider_capabilities
from soa_api.domain.rules import validate_rule_set
from soa_api.domain.schemas import validate_schema
from soa_fixtures.demo import DEMO_TENANT


def test_demo_schema_validates() -> None:
    schema = validate_schema(DEMO_TENANT.schema.data)
    assert "lines.quantity" in schema.field_types()


def test_demo_rules_validate_against_demo_schema() -> None:
    field_types = validate_schema(DEMO_TENANT.schema.data).field_types()
    rules = validate_rule_set(DEMO_TENANT.rules.data, field_types)
    assert {rule["key"] for rule in rules} == {"po-number-required", "customer-required"}


def test_demo_provider_policy_validates_and_is_publishable() -> None:
    validate_policy(PolicyType.PROVIDER, DEMO_TENANT.provider_policy.data)
    validate_provider_capabilities(DEMO_TENANT.provider_policy.data)
