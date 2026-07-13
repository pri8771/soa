"""Operations dashboard API (ANA-004).

One endpoint returns the ANA-001 operational snapshot plus a
"needs attention" list. Every attention item carries a DECLARATIVE
drill-down target (screen + filters) that the web client maps onto its
own routes — a tile that cannot take the operator to the underlying
filtered view is decoration, and decoration is not shipped. Metric
definitions travel on the payload (ANA-001), so the dashboard can show
what each number means.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_db.analytics import operational_snapshot
from soa_db.types import utcnow

router = APIRouter(tags=["analytics"])

#: Default lookback when the caller names no window.
DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 366


def _parse(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must be an ISO-8601 datetime.",
        ) from None


def _attention_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything an operator should act on NOW, each with the filtered
    view that shows the underlying items."""
    sla = snapshot["sla"]
    backlog = snapshot["backlog"]
    exceptions = snapshot["exceptions"]["documents_by_state"]
    exports = snapshot["exports"]
    failed_exports = sum(
        count
        for state, count in exports["jobs_by_state"].items()
        if state in ("failed_retryable", "failed_terminal")
    )
    items = [
        {
            "key": "overdue_reviews",
            "label": "Review tasks past their SLA",
            "count": sla["overdue_now"],
            "link": {"screen": "review", "filters": {"view": "overdue"}},
        },
        {
            "key": "blocked_reviews",
            "label": "Blocking review tasks",
            "count": backlog["review_tasks"]["blocking"],
            "link": {"screen": "review", "filters": {"view": "blocked"}},
        },
        {
            "key": "quarantined_documents",
            "label": "Quarantined documents",
            "count": exceptions.get("quarantined", 0),
            "link": {"screen": "documents", "filters": {"docState": "quarantined"}},
        },
        {
            "key": "failed_documents",
            "label": "Failed documents (retryable)",
            "count": exceptions.get("failed_retryable", 0),
            "link": {"screen": "documents", "filters": {"docState": "failed_retryable"}},
        },
        {
            "key": "terminally_failed_documents",
            "label": "Failed documents (terminal)",
            "count": exceptions.get("failed_terminal", 0),
            "link": {"screen": "documents", "filters": {"docState": "failed_terminal"}},
        },
        {
            "key": "failing_exports",
            "label": "Export jobs failing in the window",
            "count": failed_exports,
            "link": {"screen": "integrations", "filters": {}},
        },
    ]
    return items


@router.get("/orgs/{organization_slug}/analytics/operations")
async def operations_dashboard(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("documents.read"))],
    session: DbSession,
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """The ANA-004 read model: the operational snapshot for the window
    (default: the last 14 days) plus actionable attention items with
    their drill-down targets."""
    until_dt = _parse(until, "until") or utcnow()
    since_dt = _parse(since, "since") or (until_dt - timedelta(days=DEFAULT_WINDOW_DAYS))
    if since_dt >= until_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="since must be before until."
        )
    if until_dt - since_dt > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"the window cannot exceed {MAX_WINDOW_DAYS} days.",
        )
    snapshot = await operational_snapshot(
        session, authorized.org_context, since=since_dt, until=until_dt
    )
    return {**snapshot, "needs_attention": _attention_items(snapshot)}
