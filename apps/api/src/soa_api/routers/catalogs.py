"""Catalog API (CAT-004).

The permission split is deliberate: ``catalogs.read`` sees catalogs and
records, ``catalogs.manage`` creates catalogs and runs imports, and
``catalogs.activate`` — its own grant — flips which version production
matches against. Imports are synchronous and honest: the response
carries every row issue, the added/changed/deactivated preview, and the
created DRAFT version (never activated as a side effect); ``dry_run``
parses and previews without creating anything.
"""

import base64
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession
from soa_db.catalog_import import (
    CatalogImportError,
    ColumnMapping,
    ParseResult,
    apply_import,
    parse_catalog_csv,
    preview_import,
)
from soa_db.catalog_import_xlsx import parse_catalog_xlsx
from soa_db.catalogs import (
    Catalog,
    CatalogError,
    CatalogRecordRepository,
    CatalogRepository,
    CatalogVersion,
    CatalogVersionRepository,
    activate_catalog_version,
)
from soa_db.pagination import CursorRequest, InvalidCursorError, decode_cursor
from soa_db.versioning import InvalidVersionStateError

router = APIRouter(tags=["catalogs"])

MAX_IMPORT_BYTES = 10 * 1024 * 1024


class CreateCatalogRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    catalog_type: str
    source: str


class MappingBody(BaseModel):
    source_id: str
    display_name: str
    aliases: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

    def to_mapping(self) -> ColumnMapping:
        return ColumnMapping(
            source_id=self.source_id,
            display_name=self.display_name,
            aliases=self.aliases,
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            attributes=dict(self.attributes),
        )


class ImportRequest(BaseModel):
    filename: str
    content_base64: str
    mapping: MappingBody
    sheet: str | None = None
    allow_partial: bool = False
    dry_run: bool = False
    change_summary: str | None = Field(default=None, max_length=500)


def _catalog_payload(catalog: Catalog) -> dict[str, Any]:
    return {
        "id": str(catalog.id),
        "name": catalog.name,
        "slug": catalog.slug,
        "catalog_type": catalog.catalog_type,
        "source": catalog.source,
        "active_version_id": str(catalog.active_version_id) if catalog.active_version_id else None,
    }


def _version_payload(version: CatalogVersion) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "version_number": version.version_number,
        "state": version.state,
        "record_count": version.record_count,
        "change_summary": version.change_summary,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "published_by": version.published_by,
    }


def _actor(authorized: AuthorizedContext) -> str:
    return f"user:{authorized.membership.user_id}"


async def _load_catalog(
    session: DbSession, authorized: AuthorizedContext, catalog_slug: str
) -> Catalog:
    catalog = await CatalogRepository(session, authorized.org_context).get_by_slug(catalog_slug)
    if catalog is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "catalog not found")
    return catalog


@router.get("/orgs/{organization_slug}/catalogs")
async def list_catalogs(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("catalogs.read"))],
    session: DbSession,
) -> dict[str, Any]:
    page = await CatalogRepository(session, authorized.org_context).list_page(
        CursorRequest(limit=200)
    )
    return {"items": [_catalog_payload(catalog) for catalog in page.items]}


@router.post("/orgs/{organization_slug}/catalogs", status_code=status.HTTP_201_CREATED)
async def create_catalog_endpoint(
    body: CreateCatalogRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("catalogs.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    from soa_db.catalogs import create_catalog

    existing = await CatalogRepository(session, authorized.org_context).get_by_slug(body.slug)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"catalog slug {body.slug!r} is taken")
    try:
        catalog = await create_catalog(
            session,
            authorized.org_context,
            name=body.name,
            slug=body.slug,
            catalog_type=body.catalog_type,
            source=body.source,
            actor_id=_actor(authorized),
        )
    except CatalogError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    return _catalog_payload(catalog)


@router.get("/orgs/{organization_slug}/catalogs/{catalog_slug}")
async def get_catalog(
    catalog_slug: str,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("catalogs.read"))],
    session: DbSession,
) -> dict[str, Any]:
    catalog = await _load_catalog(session, authorized, catalog_slug)
    versions = await CatalogVersionRepository(session, authorized.org_context).list_for_catalog(
        catalog.id
    )
    return {
        "catalog": _catalog_payload(catalog),
        "versions": [_version_payload(version) for version in versions],
    }


def _parse_upload(body: ImportRequest) -> ParseResult:
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except Exception:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "content_base64 is not valid base64"
        ) from None
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"imports are limited to {MAX_IMPORT_BYTES // (1024 * 1024)} MiB",
        )
    lowered = body.filename.lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        return parse_catalog_xlsx(data, body.mapping.to_mapping(), sheet=body.sheet)
    if lowered.endswith(".csv"):
        return parse_catalog_csv(data, body.mapping.to_mapping())
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "unsupported file type — upload a .csv, .xlsx, or .xlsm file",
    )


@router.post("/orgs/{organization_slug}/catalogs/{catalog_slug}/imports")
async def import_catalog(
    catalog_slug: str,
    body: ImportRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("catalogs.manage"))],
    session: DbSession,
) -> dict[str, Any]:
    catalog = await _load_catalog(session, authorized, catalog_slug)
    try:
        parsed = _parse_upload(body)
    except CatalogImportError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    preview = await preview_import(session, authorized.org_context, catalog=catalog, parsed=parsed)
    payload: dict[str, Any] = {
        "status": "parsed",
        "records": len(parsed.records),
        "issues": [
            {"row_number": issue.row_number, "message": issue.message} for issue in parsed.issues
        ],
        "warnings": list(parsed.warnings),
        "encoding": parsed.encoding,
        "preview": {
            "added": list(preview.added),
            "changed": list(preview.changed),
            "deactivated": list(preview.deactivated),
            "unchanged": preview.unchanged,
        },
        "version": None,
    }
    if body.dry_run:
        payload["status"] = "previewed"
        return payload
    try:
        draft = await apply_import(
            session,
            authorized.org_context,
            catalog=catalog,
            parsed=parsed,
            change_summary=body.change_summary,
            actor_id=_actor(authorized),
            allow_partial=body.allow_partial,
        )
    except CatalogImportError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    payload["status"] = "draft_created"
    payload["version"] = _version_payload(draft)
    return payload


@router.get("/orgs/{organization_slug}/catalogs/{catalog_slug}/versions/{version_id}/records")
async def list_records(
    catalog_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("catalogs.read"))],
    session: DbSession,
    q: str | None = Query(default=None, max_length=200),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    catalog = await _load_catalog(session, authorized, catalog_slug)
    version = await CatalogVersionRepository(session, authorized.org_context).get(version_id)
    if version is None or version.catalog_id != catalog.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "catalog version not found")
    after = None
    if cursor:
        try:
            after = decode_cursor(cursor)
        except InvalidCursorError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    page = await CatalogRecordRepository(session, authorized.org_context).search_page(
        version.id, request=CursorRequest(limit=limit, after=after), q=q
    )
    return {
        "items": [
            {
                "id": str(record.id),
                "source_id": record.source_id,
                "display_name": record.display_name,
                "aliases": list(record.aliases),
                "attributes": dict(record.attributes),
                "effective_from": str(record.effective_from) if record.effective_from else None,
                "effective_to": str(record.effective_to) if record.effective_to else None,
            }
            for record in page.items
        ],
        "has_more": page.has_more,
        "next_cursor": page.next_cursor,
    }


@router.post("/orgs/{organization_slug}/catalogs/{catalog_slug}/versions/{version_id}/activate")
async def activate_version(
    catalog_slug: str,
    version_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("catalogs.activate"))],
    session: DbSession,
) -> dict[str, Any]:
    catalog = await _load_catalog(session, authorized, catalog_slug)
    version = await CatalogVersionRepository(session, authorized.org_context).get(version_id)
    if version is None or version.catalog_id != catalog.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "catalog version not found")
    try:
        activated = await activate_catalog_version(
            session,
            authorized.org_context,
            catalog=catalog,
            version=version,
            actor_id=_actor(authorized),
        )
    except CatalogError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except InvalidVersionStateError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    return _version_payload(activated)
