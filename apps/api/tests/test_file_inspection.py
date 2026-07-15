"""File signature validation tests (ING-003): mismatched and malformed
fixtures, safe reasons, end-to-end rejection/quarantine at completion."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.services.file_inspection import (
    InspectionVerdict,
    detect_content_type,
    inspect_file,
)
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import Document
from soa_storage import MemoryObjectStore, sha256_hex

PDF = b"%PDF-1.7 body"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GARBAGE = b"MZ\x90\x00 definitely not a document"


# --- unit: detection and verdicts ------------------------------------------


def test_signatures_detect_supported_types() -> None:
    assert detect_content_type(PDF) == "application/pdf"
    assert detect_content_type(PNG) == "image/png"
    assert detect_content_type(JPEG) == "image/jpeg"
    assert detect_content_type(b"II*\x00rest") == "image/tiff"
    assert detect_content_type(b"MM\x00*rest") == "image/tiff"
    assert detect_content_type(GARBAGE) is None


def test_matching_content_declaration_and_extension_passes() -> None:
    result = inspect_file(head=PDF, declared_type="application/pdf", filename="po.pdf")
    assert result.verdict is InspectionVerdict.PASSED
    assert result.reason is None


def test_type_confusion_is_rejected_with_safe_reason() -> None:
    # PNG bytes claiming to be a PDF.
    result = inspect_file(head=PNG, declared_type="application/pdf", filename="po.pdf")
    assert result.verdict is InspectionVerdict.REJECTED
    assert result.detected_type == "image/png"
    assert "type confusion" in (result.reason or "")
    # The reason names types only — no content bytes leak.
    assert "PNG" not in (result.reason or "").replace("image/png", "")


def test_extension_disagreeing_with_content_is_rejected() -> None:
    result = inspect_file(head=PDF, declared_type="application/pdf", filename="invoice.png")
    assert result.verdict is InspectionVerdict.REJECTED
    assert ".png" in (result.reason or "")
    missing = inspect_file(head=PDF, declared_type="application/pdf", filename="noextension")
    assert missing.verdict is InspectionVerdict.REJECTED


def test_unrecognized_signature_is_quarantined() -> None:
    result = inspect_file(head=GARBAGE, declared_type="application/pdf", filename="po.pdf")
    assert result.verdict is InspectionVerdict.QUARANTINED
    assert result.detected_type is None
    assert "not recognized" in (result.reason or "")


# --- end to end: completion applies the verdict -----------------------------


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions, MemoryObjectStore]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/inspection.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    store = MemoryObjectStore()
    app = create_app(ApiSettings(environment=Environment.TEST), db=db, object_store=store)
    return TestClient(app, raise_server_exceptions=False), db, store


ADMIN = {"X-Dev-User": "user:reviewer"}


async def seed_stream(client: TestClient, db: DatabaseSessions) -> None:
    for call in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(call[0], json=call[1], headers=ADMIN).status_code == 201
    await publish_runtime_config(client, db)


async def run_upload(
    client: TestClient,
    store: MemoryObjectStore,
    *,
    data: bytes,
    declared_type: str,
    filename: str,
) -> dict[str, str]:
    created = client.post(
        "/orgs/northstar/streams/email/uploads",
        json={
            "filename": filename,
            "content_type": declared_type,
            "size_bytes": len(data),
            "sha256": sha256_hex(data),
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    key = store.verify_signed_url(payload["upload_url"])
    await store.put(key, data, content_type=declared_type)
    completed = client.post(
        f"/orgs/northstar/uploads/{payload['session_id']}/complete", headers=ADMIN
    )
    assert completed.status_code == 200, completed.text
    return dict(completed.json())


async def test_type_confusion_upload_lands_rejected_with_recorded_reason(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    await seed_stream(client, db)
    # PNG bytes declared and named as PDF.
    png_as_pdf = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    result = await run_upload(
        client, store, data=png_as_pdf, declared_type="application/pdf", filename="po.pdf"
    )
    assert result["state"] == "rejected"

    async with db.session_scope() as session:
        document = (await session.execute(select(Document))).scalars().one()
        assert document.state == "rejected"
        assert "type confusion" in (document.state_reason or "")


async def test_unrecognized_bytes_land_quarantined(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, db, store = harness
    await seed_stream(client, db)
    result = await run_upload(
        client,
        store,
        data=b"MZ\x90\x00 not a supported file at all",
        declared_type="application/pdf",
        filename="po.pdf",
    )
    assert result["state"] == "quarantined"
    async with db.session_scope() as session:
        document = (await session.execute(select(Document))).scalars().one()
        assert "not recognized" in (document.state_reason or "")


async def test_honest_pdf_lands_queued(
    harness: tuple[TestClient, DatabaseSessions, MemoryObjectStore],
) -> None:
    client, _db, store = harness
    await seed_stream(client, _db)
    result = await run_upload(
        client, store, data=PDF, declared_type="application/pdf", filename="po.pdf"
    )
    assert result["state"] == "queued"
