"""Exact-duplicate policy read compatibility and strict write validation."""

import pytest

from soa_db.duplicate_policy import (
    DEFAULT_POLICY,
    DuplicatePolicy,
    DuplicatePolicyValidationError,
    get_duplicate_policy,
    validate_duplicate_policy,
)


@pytest.mark.parametrize("value", ["reject", "flag", "allow"])
def test_strict_validation_accepts_the_documented_values(value: str) -> None:
    assert validate_duplicate_policy({"duplicate_policy": value}) is DuplicatePolicy(value)


@pytest.mark.parametrize("value", ["nonsense", "FLAG", "", None, 1])
def test_strict_validation_rejects_invalid_authored_values(value: object) -> None:
    with pytest.raises(DuplicatePolicyValidationError, match="reject, flag, allow"):
        validate_duplicate_policy({"duplicate_policy": value})


def test_missing_write_and_legacy_invalid_read_keep_the_safe_default() -> None:
    assert validate_duplicate_policy({}) is DEFAULT_POLICY
    assert get_duplicate_policy({"duplicate_policy": "legacy-invalid"}) is DEFAULT_POLICY
    assert get_duplicate_policy({"duplicate_policy": None}) is DEFAULT_POLICY
