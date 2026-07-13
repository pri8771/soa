"""Retention-policy engine (SEC-008).

A pure, deterministic engine that decides WHEN a document's stored
artifacts become eligible for deletion, and gates the path to actual
deletion behind eligibility, legal hold, and explicit approval. It
computes; it never deletes — the deletion orchestration (SEC-010) drives
this engine and performs the object/database work.

Load-bearing properties:

- **the policy is PINNED, not live.** Eligibility is computed from the
  retention window that was in force when the document SETTLED
  (:class:`RetentionPolicy` captured at that moment, e.g. from the
  CFG-006 resolved config). Re-publishing a shorter policy later can
  never retroactively make already-settled documents deletable — a test
  pins this. The caller stores the pinned window; the engine consumes it.
- **legal hold always wins.** An artifact classed ``legal_hold`` or a
  document under an active hold is NEVER eligible, whatever the window
  says — and the deletion state machine refuses to advance it. Releasing
  a hold resumes the ordinary schedule; it does not delete anything.
- **deletion is a gated state machine.** ``RETAINED -> ELIGIBLE ->
  PENDING_APPROVAL -> APPROVED -> DELETED`` (with ``ELIGIBLE`` able to
  fall back to ``RETAINED`` if a hold is placed). Nothing reaches
  ``APPROVED`` without passing eligibility, and nothing reaches
  ``DELETED`` without approval — deletion never occurs before
  eligibility AND approval.
- **extended retention lengthens, never shortens.** The ``extended``
  class multiplies the base window; ``standard`` uses it as-is. A class
  can only keep data LONGER than the base, never less.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from soa_db.artifacts import RetentionClass

__all__ = [
    "DeletionState",
    "RetentionDecision",
    "RetentionPolicy",
    "RetentionStateError",
    "advance_deletion_state",
    "evaluate_retention",
    "pin_policy_from_definition",
]

#: How much longer the EXTENDED class keeps data than the base window.
_EXTENDED_MULTIPLIER = 3


class DeletionState(StrEnum):
    """The lifecycle a document's artifacts move through on the way to
    deletion. SEC-010 persists and drives these transitions."""

    RETAINED = "retained"  # within the window, or on hold — keep
    ELIGIBLE = "eligible"  # window elapsed, no hold — may be queued
    PENDING_APPROVAL = "pending_approval"  # queued, awaiting operator sign-off
    APPROVED = "approved"  # signed off — SEC-010 may delete
    DELETED = "deleted"  # terminal; artifacts gone, tombstone kept


#: Allowed transitions. A hold placed on an ELIGIBLE/PENDING item pulls
#: it back to RETAINED; nothing ever leaves DELETED.
_ALLOWED_TRANSITIONS: dict[DeletionState, frozenset[DeletionState]] = {
    DeletionState.RETAINED: frozenset({DeletionState.ELIGIBLE}),
    DeletionState.ELIGIBLE: frozenset({DeletionState.PENDING_APPROVAL, DeletionState.RETAINED}),
    DeletionState.PENDING_APPROVAL: frozenset({DeletionState.APPROVED, DeletionState.RETAINED}),
    DeletionState.APPROVED: frozenset({DeletionState.DELETED}),
    DeletionState.DELETED: frozenset(),
}


class RetentionStateError(Exception):
    """An illegal deletion-state transition was attempted."""

    def __init__(self, current: DeletionState, requested: DeletionState) -> None:
        super().__init__(f"cannot move retention state {current.value!r} -> {requested.value!r}")
        self.current = current
        self.requested = requested


@dataclass(frozen=True)
class RetentionPolicy:
    """A PINNED retention window. Captured when a document settles and
    stored with it; the engine reads it, never a live policy."""

    base_days: int

    def __post_init__(self) -> None:
        if not isinstance(self.base_days, int) or self.base_days < 1:
            raise ValueError("retention base_days must be a positive integer")

    def window_days(self, retention_class: RetentionClass) -> int | None:
        """Days to keep artifacts of ``retention_class``. ``None`` means
        never eligible (legal hold)."""
        if retention_class == RetentionClass.LEGAL_HOLD:
            return None
        if retention_class == RetentionClass.EXTENDED:
            return self.base_days * _EXTENDED_MULTIPLIER
        return self.base_days


def pin_policy_from_definition(definition: dict[str, Any]) -> RetentionPolicy:
    """Build a pinned policy from a published CFG-005 retention policy
    definition (``{"document_days": N}``). Call this at settlement and
    STORE the result — do not re-resolve it at deletion time."""
    days = definition.get("document_days")
    if not isinstance(days, int) or days < 1:
        raise ValueError("retention definition needs a positive integer document_days")
    return RetentionPolicy(base_days=days)


@dataclass(frozen=True)
class RetentionDecision:
    """The engine's verdict for one document's artifacts."""

    eligible: bool
    eligible_at: datetime | None  # None = never (legal hold)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def on_legal_hold(self) -> bool:
        return self.eligible_at is None


def evaluate_retention(
    *,
    settled_at: datetime,
    retention_class: RetentionClass,
    policy: RetentionPolicy,
    now: datetime,
    legal_hold_active: bool = False,
) -> RetentionDecision:
    """Decide whether a document's artifacts are eligible for deletion.

    ``settled_at`` is when the document reached a terminal state (the
    retention clock starts there). ``policy`` is the PINNED window.
    ``legal_hold_active`` is the caller's hook for an explicit hold that
    is separate from the artifact class (e.g. a case-level hold) — the
    engine treats either signal as an absolute block.
    """
    reasons: list[str] = []
    if retention_class == RetentionClass.LEGAL_HOLD:
        reasons.append("artifact is classified legal_hold")
    if legal_hold_active:
        reasons.append("an active legal hold covers this document")
    if retention_class == RetentionClass.LEGAL_HOLD or legal_hold_active:
        return RetentionDecision(eligible=False, eligible_at=None, reasons=tuple(reasons))

    days = policy.window_days(retention_class)
    assert days is not None  # legal hold handled above
    eligible_at = settled_at + timedelta(days=days)
    if now < eligible_at:
        remaining = (eligible_at - now).days
        reasons.append(
            f"within the {days}-day retention window "
            f"(~{max(remaining, 0)} days remain before eligibility)"
        )
        return RetentionDecision(eligible=False, eligible_at=eligible_at, reasons=tuple(reasons))
    reasons.append(f"the {days}-day retention window elapsed on {eligible_at.date().isoformat()}")
    return RetentionDecision(eligible=True, eligible_at=eligible_at, reasons=tuple(reasons))


def advance_deletion_state(
    current: DeletionState,
    requested: DeletionState,
    *,
    decision: RetentionDecision | None = None,
) -> DeletionState:
    """Validate and return a deletion-state transition.

    The two safety gates: moving to ``ELIGIBLE`` requires an eligible
    :class:`RetentionDecision` (deletion never begins before the window
    elapses), and the only route to ``DELETED`` is through ``APPROVED``
    (deletion never occurs without sign-off). Placing a hold — moving
    back to ``RETAINED`` — is always allowed from a pre-approval state.
    """
    if requested not in _ALLOWED_TRANSITIONS[current]:
        raise RetentionStateError(current, requested)
    if requested == DeletionState.ELIGIBLE:
        if decision is None or not decision.eligible:
            raise RetentionStateError(current, requested)
    return requested
