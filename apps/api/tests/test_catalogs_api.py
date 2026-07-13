"""Catalog API tests (CAT-004): the read/import/activate permission
split, CSV and XLSX imports over HTTP (dry run, issues, preview, draft),
record pagination and search, and activation."""

import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from soa_api.app import create_app
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine

ADMIN = {"X-Dev-User": "user:admin"}  # org creator -> org-admin (all grants)
SUPERVISOR = {"X-Dev-User": "user:supervisor"}  # catalogs.read only
REVIEWER = {"X-Dev-User": "user:reviewer"}  # catalogs.read only

CSV = (
    b"sku,name,aliases,valid_from,unit\n"
    b"SKU-1,Widget 9mm,WIDGET-9|Widget Nine,2026-01-01,EA\n"
    b"SKU-2,Flange Kit,,,BOX\n"
)

MAPPING = {
    "source_id": "sku",
    "display_name": "name",
    "aliases": "aliases",
    "effective_from": "valid_from",
    "attributes": {"uom": "unit"},
}


def import_body(data: bytes, filename: str = "products.csv", **extra: object) -> dict:
    return {
        "filename": filename,
        "content_base64": base64.b64encode(data).decode("ascii"),
        "mapping": MAPPING,
        **extra,
    }


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/catalogs-api.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(ApiSettings(environment=Environment.TEST), db=DatabaseSessions(engine))
    test_client = TestClient(app, raise_server_exceptions=False)
    assert (
        test_client.post(
            "/organizations", json={"name": "Northstar", "slug": "northstar"}, headers=ADMIN
        ).status_code
        == 201
    )
    for headers, email, role in (
        (SUPERVISOR, "supervisor@northstar.example", "supervisor"),
        (REVIEWER, "reviewer@northstar.example", "reviewer"),
    ):
        test_client.post("/orgs/northstar/invitations", json={"email": email}, headers=ADMIN)
        accepted = test_client.post(
            "/invitations/accept", json={"organization_slug": "northstar"}, headers=headers
        )
        granted = test_client.post(
            f"/orgs/northstar/members/{accepted.json()['membership_id']}/roles",
            json={"role_slug": role},
            headers=ADMIN,
        )
        assert granted.status_code == 201, granted.text
    created = test_client.post(
        "/orgs/northstar/catalogs",
        json={
            "name": "Products",
            "slug": "products",
            "catalog_type": "products",
            "source": "csv_import",
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    return test_client


def run_import(client: TestClient, data: bytes = CSV, **extra: object) -> dict:
    response = client.post(
        "/orgs/northstar/catalogs/products/imports",
        json=import_body(data, **extra),
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestPermissionSplit:
    def test_read_import_and_activate_are_separate_grants(self, client: TestClient) -> None:
        # Supervisor: read yes, import no, activate no.
        assert client.get("/orgs/northstar/catalogs", headers=SUPERVISOR).status_code == 200
        assert (
            client.post(
                "/orgs/northstar/catalogs/products/imports",
                json=import_body(CSV),
                headers=SUPERVISOR,
            ).status_code
            == 403
        )
        result = run_import(client)
        version_id = result["version"]["id"]
        assert (
            client.post(
                f"/orgs/northstar/catalogs/products/versions/{version_id}/activate",
                headers=SUPERVISOR,
            ).status_code
            == 403
        )
        # Reviewer holds catalogs.read too; creation needs manage.
        assert (
            client.post(
                "/orgs/northstar/catalogs",
                json={"name": "X", "slug": "x", "catalog_type": "custom", "source": "manual"},
                headers=REVIEWER,
            ).status_code
            == 403
        )


class TestImports:
    def test_csv_import_creates_a_draft_with_preview_and_no_activation(
        self, client: TestClient
    ) -> None:
        result = run_import(client)
        assert result["status"] == "draft_created"
        assert result["records"] == 2
        assert result["issues"] == []
        assert result["preview"]["added"] == ["SKU-1", "SKU-2"]
        assert result["version"]["state"] == "draft"
        detail = client.get("/orgs/northstar/catalogs/products", headers=ADMIN).json()
        assert detail["catalog"]["active_version_id"] is None  # not live

    def test_dry_run_previews_without_creating_anything(self, client: TestClient) -> None:
        result = run_import(client, dry_run=True)
        assert result["status"] == "previewed"
        assert result["version"] is None
        detail = client.get("/orgs/northstar/catalogs/products", headers=ADMIN).json()
        assert detail["versions"] == []

    def test_row_issues_are_reported_and_block_without_allow_partial(
        self, client: TestClient
    ) -> None:
        broken = CSV + b",missing id,,,EA\n"
        refused = client.post(
            "/orgs/northstar/catalogs/products/imports",
            json=import_body(broken),
            headers=ADMIN,
        )
        assert refused.status_code == 422
        assert "allow_partial" in refused.json()["error"]["message"]
        result = run_import(client, data=broken, allow_partial=True)
        assert result["status"] == "draft_created"
        assert len(result["issues"]) == 1
        assert result["issues"][0]["row_number"] == 3

    def test_xlsx_imports_use_the_same_pipeline(self, client: TestClient) -> None:
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.append(["sku", "name", "aliases", "valid_from", "unit"])
        sheet.append(["SKU-9", "Bolt", None, None, "EA"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        result = run_import(client, data=buffer.getvalue(), filename="products.xlsx")
        assert result["status"] == "draft_created"
        assert result["records"] == 1
        assert result["encoding"] == "xlsx"

    def test_unsupported_types_and_bad_base64_are_refused(self, client: TestClient) -> None:
        assert (
            client.post(
                "/orgs/northstar/catalogs/products/imports",
                json=import_body(CSV, filename="products.pdf"),
                headers=ADMIN,
            ).status_code
            == 422
        )
        body = import_body(CSV)
        body["content_base64"] = "not base64!!"
        assert (
            client.post(
                "/orgs/northstar/catalogs/products/imports", json=body, headers=ADMIN
            ).status_code
            == 422
        )


class TestRecordsAndActivation:
    def activate(self, client: TestClient) -> str:
        result = run_import(client)
        version_id = result["version"]["id"]
        activated = client.post(
            f"/orgs/northstar/catalogs/products/versions/{version_id}/activate", headers=ADMIN
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["state"] == "published"
        return version_id

    def test_activation_moves_the_pointer_and_double_activation_conflicts(
        self, client: TestClient
    ) -> None:
        version_id = self.activate(client)
        detail = client.get("/orgs/northstar/catalogs/products", headers=ADMIN).json()
        assert detail["catalog"]["active_version_id"] == version_id
        again = client.post(
            f"/orgs/northstar/catalogs/products/versions/{version_id}/activate", headers=ADMIN
        )
        assert again.status_code == 409

    def test_records_paginate_and_search(self, client: TestClient) -> None:
        version_id = self.activate(client)
        base = f"/orgs/northstar/catalogs/products/versions/{version_id}/records"
        first = client.get(f"{base}?limit=1", headers=SUPERVISOR)
        assert first.status_code == 200
        page = first.json()
        assert len(page["items"]) == 1 and page["has_more"] is True
        second = client.get(f"{base}?limit=1&cursor={page['next_cursor']}", headers=SUPERVISOR)
        assert len(second.json()["items"]) == 1
        # Search hits source ids, names, and aliases.
        by_alias = client.get(f"{base}?q=widget nine", headers=SUPERVISOR).json()
        assert [item["source_id"] for item in by_alias["items"]] == ["SKU-1"]
        by_name = client.get(f"{base}?q=flange", headers=SUPERVISOR).json()
        assert [item["source_id"] for item in by_name["items"]] == ["SKU-2"]
        none = client.get(f"{base}?q=unicorn", headers=SUPERVISOR).json()
        assert none["items"] == []
