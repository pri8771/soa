"""Stream duplicate policy (ING-006) — shared by API intake and worker.

Stream configuration chooses what happens to an exact duplicate via
``duplicate_policy``:

- ``reject``  — the duplicate is refused (state rejected, reason names
  the original document).
- ``flag``    — the duplicate proceeds, marked with ``duplicate_of``
  (default: a human decides in review).
- ``allow``   — the duplicate proceeds and is processing-transparent:
  still marked and audited (never silent), but validation must not
  route it to review for being a duplicate.

Lives in ``soa_db`` because both the API's intake service and the
worker's validation stage resolve the policy from the same stream
configuration, and the worker never imports ``soa_api``.
"""

from collections.abc import Mapping
from enum import StrEnum


class DuplicatePolicy(StrEnum):
    REJECT = "reject"
    FLAG = "flag"
    ALLOW = "allow"


DEFAULT_POLICY = DuplicatePolicy.FLAG
EXACT_DUPLICATE_RULE_KEY = "duplicates.business_hook"


class DuplicatePolicyValidationError(ValueError):
    """A newly-authored exact-duplicate policy is not part of the contract."""


def validate_duplicate_policy(stream_config: Mapping[str, object]) -> DuplicatePolicy:
    """Strictly validate policy configuration at draft/publish boundaries.

    Missing values intentionally resolve to the documented default. An explicitly
    authored value must be one of the enum strings; unlike the runtime reader below,
    this function never turns an invalid write into ``flag``.
    """

    if "duplicate_policy" not in stream_config:
        return DEFAULT_POLICY
    raw = stream_config["duplicate_policy"]
    if not isinstance(raw, str):
        raise DuplicatePolicyValidationError("duplicate_policy must be one of: reject, flag, allow")
    try:
        return DuplicatePolicy(raw)
    except ValueError as exc:
        raise DuplicatePolicyValidationError(
            "duplicate_policy must be one of: reject, flag, allow"
        ) from exc


def get_duplicate_policy(stream_config: Mapping[str, object]) -> DuplicatePolicy:
    """Read a persisted policy, falling back for legacy invalid snapshots only."""

    raw = stream_config.get("duplicate_policy")
    try:
        return DuplicatePolicy(str(raw))
    except ValueError:
        return DEFAULT_POLICY


def exact_duplicate_review_reason() -> dict[str, object | None]:
    """Stable review reason when ``flag`` must be enforced outside tenant rules."""

    return {
        "code": "exact_duplicate_flagged",
        "severity": "warning",
        "message": "Ingestion flagged this document as an exact duplicate",
        "field_key": None,
        "row_index": None,
        "rule_key": EXACT_DUPLICATE_RULE_KEY,
    }


__all__ = [
    "DEFAULT_POLICY",
    "EXACT_DUPLICATE_RULE_KEY",
    "DuplicatePolicy",
    "DuplicatePolicyValidationError",
    "exact_duplicate_review_reason",
    "get_duplicate_policy",
    "validate_duplicate_policy",
]
