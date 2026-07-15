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


def get_duplicate_policy(stream_config: Mapping[str, object]) -> DuplicatePolicy:
    raw = stream_config.get("duplicate_policy")
    try:
        return DuplicatePolicy(str(raw))
    except ValueError:
        return DEFAULT_POLICY
