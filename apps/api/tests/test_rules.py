"""Rule-set expression parser/type/evaluator baseline (CFG-004)."""

import uuid
from pathlib import Path

import pytest

from soa_api.domain.rules import (
    RuleExpressionError,
    RuleSetVersionRepository,
    create_rule_set_draft,
    evaluate,
    publish_rule_set_draft,
    type_check,
    validate_rule_set,
)
from soa_api.domain.versioning import ImmutableVersionError
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ACTOR = "user:test-admin"
PROCESS_ID = uuid.UUID(int=0x88)

FIELD_TYPES = {"total": "money", "quantity": "number", "po_number": "text", "currency": "enum"}

HIGH_TOTAL = {
    "op": "and",
    "args": [
        {"op": "is_present", "key": "total"},
        {
            "op": "gt",
            "left": {"op": "field", "key": "total"},
            "right": {"op": "const", "value": 10000},
        },
    ],
}

RULE_SET = {
    "rules": [
        {
            "key": "high-value-review",
            "severity": "warning",
            "action": "route_to_review",
            "condition": HIGH_TOTAL,
            "test_cases": [
                {"values": {"total": 20000}, "expect_triggered": True},
                {"values": {"total": 50}, "expect_triggered": False},
                {"values": {}, "expect_triggered": False},
            ],
        },
        {
            "key": "po-number-required",
            "severity": "error",
            "action": "block",
            "condition": {"op": "not", "arg": {"op": "is_present", "key": "po_number"}},
        },
    ]
}


def test_type_checker_accepts_valid_and_rejects_invalid_expressions() -> None:
    type_check(HIGH_TOTAL, FIELD_TYPES)
    with pytest.raises(RuleExpressionError, match="unknown field"):
        type_check({"op": "is_present", "key": "nope"}, FIELD_TYPES)
    with pytest.raises(RuleExpressionError, match="cannot compare"):
        type_check(
            {
                "op": "eq",
                "left": {"op": "field", "key": "po_number"},
                "right": {"op": "field", "key": "quantity"},
            },
            FIELD_TYPES,
        )
    with pytest.raises(RuleExpressionError, match="ordered type"):
        type_check(
            {
                "op": "gt",
                "left": {"op": "field", "key": "po_number"},
                "right": {"op": "const", "value": "x"},
            },
            FIELD_TYPES,
        )
    with pytest.raises(RuleExpressionError, match="unknown op"):
        type_check({"op": "exec", "code": "os.system"}, FIELD_TYPES)
    with pytest.raises(RuleExpressionError, match="must evaluate to a boolean"):
        type_check({"op": "field", "key": "total"}, FIELD_TYPES)


def test_evaluator_is_deterministic_and_fails_safe_on_missing_fields() -> None:
    assert evaluate(HIGH_TOTAL, {"total": 10001}) is True
    assert evaluate(HIGH_TOTAL, {"total": 10000}) is False
    assert evaluate(HIGH_TOTAL, {}) is False, "missing field comparisons fail safe"
    not_present = {"op": "not", "arg": {"op": "is_present", "key": "po_number"}}
    assert evaluate(not_present, {}) is True
    assert evaluate(not_present, {"po_number": "PO-1"}) is False


def test_expressions_are_pure_data_round_trippable() -> None:
    import json

    assert json.loads(json.dumps(HIGH_TOTAL)) == HIGH_TOTAL


def test_rule_set_validation_enforces_structure_and_test_cases() -> None:
    validate_rule_set(RULE_SET, FIELD_TYPES)
    with pytest.raises(RuleExpressionError, match="duplicate rule key"):
        validate_rule_set({"rules": [RULE_SET["rules"][0], RULE_SET["rules"][0]]}, FIELD_TYPES)
    with pytest.raises(RuleExpressionError, match="invalid severity"):
        validate_rule_set(
            {"rules": [{**RULE_SET["rules"][1], "key": "x", "severity": "fatal"}]}, FIELD_TYPES
        )
    failing_case = {
        "rules": [
            {
                "key": "broken",
                "severity": "error",
                "action": "block",
                "condition": HIGH_TOTAL,
                "test_cases": [{"values": {"total": 1}, "expect_triggered": True}],
            }
        ]
    }
    with pytest.raises(RuleExpressionError, match="fix the rule or the test case"):
        validate_rule_set(failing_case, FIELD_TYPES)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/rules.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_rule_set_publish_lifecycle_and_immutability(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        draft = await create_rule_set_draft(
            session,
            ORG_A,
            process_id=PROCESS_ID,
            definition=RULE_SET,
            field_types=FIELD_TYPES,
            actor_id=ACTOR,
        )
        await publish_rule_set_draft(
            session, ORG_A, draft=draft, field_types=FIELD_TYPES, actor_id=ACTOR
        )
        published_id = draft.id
    with pytest.raises(ImmutableVersionError):
        async with db.session_scope() as session:
            stored = await RuleSetVersionRepository(session, ORG_A).get(published_id)
            assert stored is not None
            stored.definition = {"rules": []}


async def test_invalid_rule_sets_cannot_be_drafted(db: DatabaseSessions) -> None:
    with pytest.raises(RuleExpressionError):
        async with db.session_scope() as session:
            await create_rule_set_draft(
                session,
                ORG_A,
                process_id=PROCESS_ID,
                definition={
                    "rules": [
                        {
                            "key": "x",
                            "severity": "error",
                            "action": "block",
                            "condition": {"op": "exec"},
                        }
                    ]
                },
                field_types=FIELD_TYPES,
                actor_id=ACTOR,
            )
