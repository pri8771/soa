"""Rule-set versions with a deterministic typed expression AST (CFG-004).

Rules are DATA, never code: a rule's condition is a small typed expression
tree serialized as JSON — no eval, no arbitrary execution, fully
inspectable. Expressions type-check against a schema's field types at
authoring time, and evaluation is deterministic over a document's field
values.

Node forms (dicts with an ``op`` discriminator):

    {"op": "field", "key": "total"}                      -> field value
    {"op": "const", "value": 100}                        -> literal
    {"op": "eq"|"ne"|"gt"|"gte"|"lt"|"lte", "left": N, "right": N}
    {"op": "and"|"or", "args": [N, ...]}
    {"op": "not", "arg": N}
    {"op": "is_present", "key": "po_number"}             -> field extracted?

Each rule: condition (must type-check to boolean), severity, action, and
authored test cases that must pass before the rule set publishes.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from soa_api.domain.versioning import (
    ImmutablePublishedVersionMixin,
    InvalidVersionStateError,
    VersionState,
)
from soa_db import Base, TimestampMixin, UuidPrimaryKeyMixin, VersionedMixin
from soa_db.audit import ActorType, record_audit_event
from soa_db.outbox import PORTABLE_JSON
from soa_db.repository import OrganizationContext, OrganizationScopedMixin, ScopedRepository
from soa_db.types import GUID, UTCDateTime, utcnow


class RuleSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RuleAction(StrEnum):
    BLOCK = "block"  # document cannot export until resolved
    ROUTE_TO_REVIEW = "route_to_review"
    ANNOTATE = "annotate"


class RuleExpressionError(ValueError):
    """Parse or type error in a rule expression."""


COMPARISONS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
ORDERED_TYPES = frozenset({"number", "money", "date"})


def _type_of(node: Any, field_types: dict[str, str], path: str) -> str:
    """Return the node's result type, raising RuleExpressionError on any
    malformed or ill-typed subtree. Types: boolean|number|text|money|date|enum."""
    if not isinstance(node, dict) or "op" not in node:
        raise RuleExpressionError(f"{path}: expected an expression object with 'op'")
    op = node["op"]
    if op == "field":
        key = node.get("key")
        if key not in field_types:
            raise RuleExpressionError(f"{path}: unknown field {key!r}")
        return field_types[key]
    if op == "is_present":
        key = node.get("key")
        if key not in field_types:
            raise RuleExpressionError(f"{path}: unknown field {key!r}")
        return "boolean"
    if op == "const":
        value = node.get("value")
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int | float):
            return "number"
        if isinstance(value, str):
            return "text"
        raise RuleExpressionError(f"{path}: unsupported literal {value!r}")
    if op in COMPARISONS:
        left = _type_of(node.get("left"), field_types, f"{path}.left")
        right = _type_of(node.get("right"), field_types, f"{path}.right")
        # Same type always compares; number/money interoperate; enum values
        # compare with text ONLY for equality — never ordering, never with
        # numbers.
        comparable = (
            left == right
            or {left, right} <= {"number", "money"}
            or (op in ("eq", "ne") and {left, right} <= {"enum", "text"})
        )
        if not comparable:
            raise RuleExpressionError(f"{path}: cannot compare {left} with {right}")
        if op not in ("eq", "ne") and left not in ORDERED_TYPES:
            raise RuleExpressionError(f"{path}: {op} requires an ordered type, got {left}")
        return "boolean"
    if op in ("and", "or"):
        args = node.get("args")
        if not isinstance(args, list) or len(args) < 2:
            raise RuleExpressionError(f"{path}: {op} needs at least two args")
        for index, arg in enumerate(args):
            if _type_of(arg, field_types, f"{path}.args[{index}]") != "boolean":
                raise RuleExpressionError(f"{path}.args[{index}]: {op} args must be boolean")
        return "boolean"
    if op == "not":
        if _type_of(node.get("arg"), field_types, f"{path}.arg") != "boolean":
            raise RuleExpressionError(f"{path}.arg: not requires a boolean")
        return "boolean"
    raise RuleExpressionError(f"{path}: unknown op {op!r}")


def type_check(condition: dict[str, Any], field_types: dict[str, str]) -> None:
    if _type_of(condition, field_types, "$") != "boolean":
        raise RuleExpressionError("$: rule condition must evaluate to a boolean")


def evaluate(condition: dict[str, Any], values: dict[str, Any]) -> bool:
    """Deterministically evaluate a type-checked condition over field
    values. Missing fields make comparisons False (fail safe), never crash."""
    result = _eval(condition, values)
    return bool(result)


def _eval(node: dict[str, Any], values: dict[str, Any]) -> Any:
    op = node["op"]
    if op == "field":
        return values.get(node["key"])
    if op == "is_present":
        return values.get(node["key"]) is not None
    if op == "const":
        return node["value"]
    if op in COMPARISONS:
        left = _eval(node["left"], values)
        right = _eval(node["right"], values)
        if left is None or right is None:
            return op == "ne" and (left is None) != (right is None)
        try:
            if op == "eq":
                return left == right
            if op == "ne":
                return left != right
            if op == "gt":
                return left > right
            if op == "gte":
                return left >= right
            if op == "lt":
                return left < right
            return left <= right
        except TypeError:
            # Runtime values can disagree with declared field types (bad
            # extraction, malformed test case). Fail safe, never crash.
            return False
    if op == "and":
        return all(_eval(arg, values) for arg in node["args"])
    if op == "or":
        return any(_eval(arg, values) for arg in node["args"])
    return not _eval(node["arg"], values)  # "not"


def validate_rule_set(
    definition: dict[str, Any], field_types: dict[str, str]
) -> list[dict[str, Any]]:
    """Validate a rule-set definition: structure, types, and authored test
    cases. Returns the validated rules list."""
    rules = definition.get("rules")
    if not isinstance(rules, list):
        raise RuleExpressionError("rule set must contain a 'rules' list")
    seen_keys: set[str] = set()
    for index, rule in enumerate(rules):
        path = f"rules[{index}]"
        if not isinstance(rule, dict):
            raise RuleExpressionError(f"{path}: each rule must be an object")
        key = rule.get("key")
        if not isinstance(key, str) or not key:
            raise RuleExpressionError(f"{path}: rule needs a non-empty 'key'")
        if key in seen_keys:
            raise RuleExpressionError(f"{path}: duplicate rule key {key!r}")
        seen_keys.add(key)
        if rule.get("severity") not in {s.value for s in RuleSeverity}:
            raise RuleExpressionError(f"{path}: invalid severity {rule.get('severity')!r}")
        if rule.get("action") not in {a.value for a in RuleAction}:
            raise RuleExpressionError(f"{path}: invalid action {rule.get('action')!r}")
        condition = rule.get("condition")
        if not isinstance(condition, dict):
            raise RuleExpressionError(f"{path}: missing condition expression")
        type_check(condition, field_types)
        test_cases = rule.get("test_cases", [])
        if not isinstance(test_cases, list):
            raise RuleExpressionError(f"{path}: test_cases must be a list")
        for case_index, case in enumerate(test_cases):
            if not isinstance(case, dict):
                raise RuleExpressionError(
                    f"{path}.test_cases[{case_index}]: each test case must be an object"
                )
            values = case.get("values", {})
            expected = case.get("expect_triggered")
            if not isinstance(expected, bool):
                raise RuleExpressionError(
                    f"{path}.test_cases[{case_index}]: expect_triggered must be true/false"
                )
            actual = evaluate(condition, values)
            if actual != expected:
                raise RuleExpressionError(
                    f"{path}.test_cases[{case_index}]: expected triggered={expected}, "
                    f"got {actual} — fix the rule or the test case"
                )
    return rules


class RuleSetVersion(
    UuidPrimaryKeyMixin,
    OrganizationScopedMixin,
    ImmutablePublishedVersionMixin,
    TimestampMixin,
    VersionedMixin,
    Base,
):
    __tablename__ = "rule_set_versions"

    process_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(PORTABLE_JSON, nullable=False, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("process_id", "version_number"),
        # At most ONE published version can exist at a time — the
        # database backstops the supersede logic against concurrent
        # first publishes (no prior row for optimistic locking to trip).
        Index(
            "uq_rule_set_versions_single_published",
            "process_id",
            unique=True,
            postgresql_where=text("state = 'published'"),
            sqlite_where=text("state = 'published'"),
        ),
    )


class RuleSetVersionRepository(ScopedRepository[RuleSetVersion]):
    model = RuleSetVersion

    async def list_for_process(self, process_id: uuid.UUID) -> list[RuleSetVersion]:
        stmt = (
            self._scoped_select()
            .where(RuleSetVersion.process_id == process_id)
            .order_by(RuleSetVersion.version_number)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_published(self, process_id: uuid.UUID) -> RuleSetVersion | None:
        stmt = self._scoped_select().where(
            RuleSetVersion.process_id == process_id,
            RuleSetVersion.state == VersionState.PUBLISHED,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def create_rule_set_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    process_id: uuid.UUID,
    definition: dict[str, Any],
    field_types: dict[str, str],
    change_summary: str | None = None,
    actor_id: str,
) -> RuleSetVersion:
    validate_rule_set(definition, field_types)
    repo = RuleSetVersionRepository(session, context)
    versions = await repo.list_for_process(process_id)
    next_number = (versions[-1].version_number + 1) if versions else 1
    draft = repo.add(
        RuleSetVersion(
            process_id=process_id,
            version_number=next_number,
            definition=dict(definition),
            change_summary=change_summary,
        )
    )
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="rule_set.draft_created",
        target_type="rule_set_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={"process_id": str(process_id), "version_number": next_number},
    )
    return draft


async def publish_rule_set_draft(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    draft: RuleSetVersion,
    field_types: dict[str, str],
    actor_id: str,
    now: datetime | None = None,
) -> RuleSetVersion:
    if draft.state != VersionState.DRAFT:
        raise InvalidVersionStateError(f"only drafts publish; this version is {draft.state!r}")
    validate_rule_set(draft.definition, field_types)  # re-check at publish
    repo = RuleSetVersionRepository(session, context)
    previous = await repo.get_published(draft.process_id)
    if previous is not None:
        previous.state = VersionState.SUPERSEDED
        # Flush the supersede before publishing: the single-published
        # unique index must never see two published rows mid-flush.
        await session.flush()
    draft.state = VersionState.PUBLISHED
    draft.published_at = now or utcnow()
    draft.published_by = actor_id
    await session.flush()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_id,
        action="rule_set.version_published",
        target_type="rule_set_version",
        target_id=str(draft.id),
        organization_id=context.organization_id,
        summary={
            "process_id": str(draft.process_id),
            "version_number": draft.version_number,
            "superseded_version_id": str(previous.id) if previous else None,
        },
    )
    return draft
