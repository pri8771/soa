"""Alert catalog validation (REL-007).

The catalog is the source of truth for what we alert on and who owns it,
so it is tested like code: every alert must carry an owner, a rationale,
a runbook, and a test signal; every one of the nine required alert
classes must be present; keys are unique; and live signals name a real
``soa.*`` observable while pending ones name the blocking task.
"""

from soa_config.alerts import (
    CATALOG,
    AlertCategory,
    AlertDefinition,
    AlertSeverity,
    Owner,
    runbook_slugs,
)


def test_every_required_category_is_covered() -> None:
    covered = {alert.category for alert in CATALOG}
    assert covered == set(AlertCategory), "every REL-007 alert class must have a definition"


def test_alert_keys_are_unique() -> None:
    keys = [alert.key for alert in CATALOG]
    assert len(keys) == len(set(keys))


def test_every_alert_is_fully_specified() -> None:
    for alert in CATALOG:
        assert isinstance(alert, AlertDefinition)
        assert isinstance(alert.severity, AlertSeverity)
        assert isinstance(alert.owner, Owner)
        # No alert ships without the accountability fields REL-007 requires.
        for field_name in ("title", "condition", "rationale", "runbook", "test_signal"):
            value = getattr(alert, field_name)
            assert value and value.strip(), f"{alert.key} missing {field_name}"
        # A rationale is a sentence, not a placeholder.
        assert len(alert.rationale) > 40, f"{alert.key} rationale is too thin"


def test_live_signals_are_metrics_and_pending_signals_name_a_task() -> None:
    for alert in CATALOG:
        if alert.signal_is_live:
            assert alert.signal.startswith("soa"), (
                f"{alert.key}: a live signal should name a real soa.* observable"
            )
        else:
            # pending:<TASK> — honest that the signal is not emitted yet.
            assert alert.signal.startswith("pending:")
            assert alert.signal.split(":", 1)[1].strip(), (
                f"{alert.key} pending signal names no task"
            )


def test_runbook_slugs_are_the_referenced_set() -> None:
    slugs = runbook_slugs()
    assert slugs == {alert.runbook for alert in CATALOG}
    # Slugs are filename-safe (REL-008 creates docs/runbooks/<slug>.md).
    for slug in slugs:
        assert slug and all(ch.islower() or ch in "-" for ch in slug)


def test_security_owns_the_auth_anomaly_alert() -> None:
    auth = next(a for a in CATALOG if a.category is AlertCategory.AUTH_ANOMALY)
    assert auth.owner is Owner.SECURITY_ONCALL
