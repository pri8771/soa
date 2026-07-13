"""Retention-policy engine tests (SEC-008): pinned windows, extended
retention lengthening, the legal-hold absolute block, scheduled
eligibility, and the gated deletion state machine (deletion never before
eligibility AND approval)."""

from datetime import UTC, datetime, timedelta

import pytest

from soa_db.artifacts import RetentionClass
from soa_db.retention import (
    DeletionState,
    RetentionDecision,
    RetentionPolicy,
    RetentionStateError,
    advance_deletion_state,
    evaluate_retention,
    pin_policy_from_definition,
)

SETTLED = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = RetentionPolicy(base_days=30)


def evaluate(
    retention_class: RetentionClass,
    *,
    now: datetime,
    policy: RetentionPolicy = POLICY,
    legal_hold_active: bool = False,
) -> RetentionDecision:
    return evaluate_retention(
        settled_at=SETTLED,
        retention_class=retention_class,
        policy=policy,
        now=now,
        legal_hold_active=legal_hold_active,
    )


class TestPolicy:
    def test_pins_from_a_published_definition(self) -> None:
        assert pin_policy_from_definition({"document_days": 90}) == RetentionPolicy(base_days=90)

    @pytest.mark.parametrize("definition", [{}, {"document_days": 0}, {"document_days": "30"}])
    def test_invalid_definitions_are_rejected(self, definition: dict) -> None:
        with pytest.raises(ValueError):
            pin_policy_from_definition(definition)

    def test_extended_lengthens_and_never_shortens(self) -> None:
        assert POLICY.window_days(RetentionClass.STANDARD) == 30
        assert POLICY.window_days(RetentionClass.EXTENDED) == 90
        assert POLICY.window_days(RetentionClass.EXTENDED) > POLICY.window_days(
            RetentionClass.STANDARD
        )
        assert POLICY.window_days(RetentionClass.LEGAL_HOLD) is None


class TestScheduledEligibility:
    def test_not_eligible_inside_the_window(self) -> None:
        decision = evaluate(RetentionClass.STANDARD, now=SETTLED + timedelta(days=29))
        assert not decision.eligible
        assert decision.eligible_at == SETTLED + timedelta(days=30)
        assert "retention window" in decision.reasons[0]

    def test_eligible_once_the_window_elapses(self) -> None:
        decision = evaluate(RetentionClass.STANDARD, now=SETTLED + timedelta(days=31))
        assert decision.eligible
        assert not decision.on_legal_hold

    def test_extended_class_stays_retained_longer(self) -> None:
        at_60 = SETTLED + timedelta(days=60)
        assert evaluate(RetentionClass.STANDARD, now=at_60).eligible
        assert not evaluate(RetentionClass.EXTENDED, now=at_60).eligible
        assert evaluate(RetentionClass.EXTENDED, now=SETTLED + timedelta(days=91)).eligible

    def test_published_policy_is_pinned_not_live(self) -> None:
        # A document settled under a 30-day policy is eligible at day 31.
        far_future = SETTLED + timedelta(days=400)
        assert evaluate(RetentionClass.STANDARD, now=far_future).eligible
        # Re-publishing a LONGER policy later must not un-delete it: the
        # engine reads the PINNED window, so eligibility is unchanged even
        # if a fresh 365-day policy exists elsewhere. The pinned value is
        # the only input.
        pinned = evaluate(RetentionClass.STANDARD, now=far_future, policy=RetentionPolicy(30))
        assert pinned.eligible
        # And a document pinned to 365 days is NOT eligible at day 100,
        # proving the window travels with the document, not the clock.
        long_pinned = evaluate(
            RetentionClass.STANDARD,
            now=SETTLED + timedelta(days=100),
            policy=RetentionPolicy(365),
        )
        assert not long_pinned.eligible


class TestLegalHold:
    def test_legal_hold_class_is_never_eligible(self) -> None:
        decision = evaluate(RetentionClass.LEGAL_HOLD, now=SETTLED + timedelta(days=100_000))
        assert not decision.eligible
        assert decision.on_legal_hold
        assert decision.eligible_at is None

    def test_active_hold_blocks_an_otherwise_eligible_document(self) -> None:
        # Window long elapsed, but a case-level hold is active.
        decision = evaluate(
            RetentionClass.STANDARD, now=SETTLED + timedelta(days=999), legal_hold_active=True
        )
        assert not decision.eligible
        assert decision.on_legal_hold
        assert any("legal hold" in reason for reason in decision.reasons)

    def test_releasing_a_hold_resumes_the_ordinary_schedule(self) -> None:
        after = SETTLED + timedelta(days=31)
        assert not evaluate(RetentionClass.STANDARD, now=after, legal_hold_active=True).eligible
        # Hold released (flag now False) -> ordinary eligibility returns.
        assert evaluate(RetentionClass.STANDARD, now=after, legal_hold_active=False).eligible


class TestDeletionStateMachine:
    def _eligible(self) -> RetentionDecision:
        return evaluate(RetentionClass.STANDARD, now=SETTLED + timedelta(days=31))

    def _not_eligible(self) -> RetentionDecision:
        return evaluate(RetentionClass.STANDARD, now=SETTLED + timedelta(days=1))

    def test_full_path_to_deletion_requires_eligibility_and_approval(self) -> None:
        state = DeletionState.RETAINED
        state = advance_deletion_state(state, DeletionState.ELIGIBLE, decision=self._eligible())
        state = advance_deletion_state(state, DeletionState.PENDING_APPROVAL)
        state = advance_deletion_state(state, DeletionState.APPROVED)
        state = advance_deletion_state(state, DeletionState.DELETED)
        assert state == DeletionState.DELETED

    def test_cannot_become_eligible_before_the_window(self) -> None:
        with pytest.raises(RetentionStateError):
            advance_deletion_state(
                DeletionState.RETAINED, DeletionState.ELIGIBLE, decision=self._not_eligible()
            )
        with pytest.raises(RetentionStateError):
            advance_deletion_state(DeletionState.RETAINED, DeletionState.ELIGIBLE, decision=None)

    def test_deletion_only_follows_approval(self) -> None:
        # No skipping straight from eligible/pending to deleted.
        for premature in (
            DeletionState.RETAINED,
            DeletionState.ELIGIBLE,
            DeletionState.PENDING_APPROVAL,
        ):
            with pytest.raises(RetentionStateError):
                advance_deletion_state(premature, DeletionState.DELETED, decision=self._eligible())

    def test_a_hold_pulls_a_pending_item_back_to_retained(self) -> None:
        assert (
            advance_deletion_state(DeletionState.PENDING_APPROVAL, DeletionState.RETAINED)
            == DeletionState.RETAINED
        )
        assert (
            advance_deletion_state(
                DeletionState.ELIGIBLE, DeletionState.RETAINED, decision=self._eligible()
            )
            == DeletionState.RETAINED
        )

    def test_deleted_is_terminal(self) -> None:
        for target in DeletionState:
            with pytest.raises(RetentionStateError):
                advance_deletion_state(DeletionState.DELETED, target)
