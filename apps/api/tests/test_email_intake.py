"""Email intake tests (ING-014): MIME fixtures, routing, loop
prevention, per-attachment outcomes, safe metadata, webhook security,
Mailpit adapter against a mocked transport."""

from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from soa_api.app import create_app
from soa_api.services.email_intake import MailpitPoller, parse_inbound_email, route_recipient
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import Document
from soa_storage import MemoryObjectStore

ADMIN = {"X-Dev-User": "user:reviewer"}
SECRET = "intake-shared-secret-0123456789"
PDF = b"%PDF-1.7 emailed purchase order"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def build_mime(
    *,
    to: str = "northstar.email@intake.example",
    sender: str = "ap@customer.example",
    attachments: list[tuple[str, str, bytes]] | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"Accounts Payable <{sender}>"
    message["To"] = to
    message["Subject"] = "PO 4711 attached"
    message["Message-ID"] = "<po-4711@customer.example>"
    for key, value in (headers or {}).items():
        message[key] = value
    message.set_content("Please process the attached order.\nBody text is never stored.")
    for filename, content_type, data in attachments or []:
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return bytes(message)


# --- contract ---------------------------------------------------------------


def test_mime_parsing_and_routing() -> None:
    raw = build_mime(attachments=[("po.pdf", "application/pdf", PDF)])
    email = parse_inbound_email(raw)
    assert email.sender == "ap@customer.example"
    assert email.recipient == "northstar.email@intake.example"
    assert email.subject == "PO 4711 attached"
    assert email.message_id == "<po-4711@customer.example>"
    assert not email.auto_generated
    (attachment,) = email.attachments
    assert (attachment.filename, attachment.content_type) == ("po.pdf", "application/pdf")
    assert attachment.data == PDF

    assert route_recipient("northstar.email@intake.example") == ("northstar", "email")
    assert route_recipient("not-a-stream@intake.example") is None
    assert route_recipient("nodomain") is None


def test_auto_generated_mail_is_flagged() -> None:
    for headers in (
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "bulk"},
        {"X-Auto-Response-Suppress": "All"},
    ):
        email = parse_inbound_email(build_mime(headers=headers))
        assert email.auto_generated, headers


# --- end to end -------------------------------------------------------------


@pytest.fixture
async def harness(tmp_path: Path) -> tuple[TestClient, DatabaseSessions]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/email-intake.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    app = create_app(
        ApiSettings(environment=Environment.TEST, email_intake_secret=SECRET),
        db=db,
        object_store=MemoryObjectStore(),
    )
    return TestClient(app, raise_server_exceptions=False), db


def seed_stream(client: TestClient) -> None:
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201


def deliver(client: TestClient, raw: bytes, *, secret: str = SECRET):
    return client.post("/v1/inbound-email", content=raw, headers={"X-Intake-Secret": secret})


async def test_multi_attachment_email_ingests_each_supported_file(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    seed_stream(client)
    raw = build_mime(
        attachments=[
            ("po.pdf", "application/pdf", PDF),
            ("scan.png", "image/png", PNG),
            ("macro.docm", "application/vnd.ms-word.document.macroEnabled.12", b"MZ..."),
            ("empty.pdf", "application/pdf", b""),
        ]
    )
    response = deliver(client, raw)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["outcome"] == "processed"
    outcomes = {item["filename"]: item for item in report["attachments"]}
    assert outcomes["po.pdf"]["outcome"] == "ingested"
    assert outcomes["po.pdf"]["state"] == "queued"
    assert outcomes["scan.png"]["outcome"] == "ingested"
    assert outcomes["macro.docm"]["outcome"] == "unsupported"
    assert outcomes["empty.pdf"]["outcome"] == "empty"

    async with db.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert len(documents) == 2
        for document in documents:
            assert document.source_channel == "email"
            # Safe metadata only: sender/subject/message id — never body.
            assert document.source_metadata["sender"] == "ap@customer.example"
            assert document.source_metadata["subject"] == "PO 4711 attached"
            assert "Body text" not in str(document.source_metadata)


async def test_unroutable_empty_and_looping_mail_is_explicit(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    seed_stream(client)

    unroutable = deliver(client, build_mime(to="unknown-org.email@intake.example"))
    assert unroutable.json()["outcome"] == "skipped"
    assert "no active stream" in unroutable.json()["reason"]

    empty = deliver(client, build_mime())
    assert empty.json() == {"outcome": "processed", "reason": "no attachments", "attachments": []}

    loop = deliver(
        client,
        build_mime(
            attachments=[("po.pdf", "application/pdf", PDF)],
            headers={"Auto-Submitted": "auto-replied"},
        ),
    )
    assert loop.json()["outcome"] == "skipped"
    assert "loop prevention" in loop.json()["reason"]

    async with db.session_scope() as session:
        assert (await session.execute(select(Document))).scalars().all() == []


async def test_webhook_requires_the_shared_secret(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, _db = harness
    seed_stream(client)
    raw = build_mime(attachments=[("po.pdf", "application/pdf", PDF)])
    assert deliver(client, raw, secret="wrong").status_code == 401
    assert client.post("/v1/inbound-email", content=raw).status_code == 401


async def test_unconfigured_intake_is_disabled(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/no-intake.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(environment=Environment.TEST),  # no email_intake_secret
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/inbound-email", content=b"x", headers={"X-Intake-Secret": "anything"}
        )
        assert response.status_code == 503


# --- Mailpit adapter ---------------------------------------------------------


async def test_mailpit_poller_drains_and_deletes() -> None:
    import httpx

    raw_message = build_mime(attachments=[("po.pdf", "application/pdf", PDF)])
    deleted: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/messages":
            return httpx.Response(200, json={"messages": [{"ID": "msg-1"}, {"ID": "msg-2"}]})
        if request.method == "GET" and request.url.path.startswith("/api/v1/message/"):
            return httpx.Response(200, content=raw_message)
        if request.method == "DELETE" and request.url.path == "/api/v1/messages":
            deleted.append(dict(IDs=request.read().decode()))
            return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(respond)
    poller = MailpitPoller(base_url="http://mailpit.test")

    # Inject the mock transport through httpx's client constructor.
    original_client = httpx.AsyncClient

    class PatchedClient(original_client):  # type: ignore[valid-type,misc]
        def __init__(self, **kwargs: object) -> None:
            super().__init__(transport=transport, **kwargs)  # type: ignore[arg-type]

    httpx.AsyncClient = PatchedClient  # type: ignore[misc]
    try:
        drained = await poller.drain()
    finally:
        httpx.AsyncClient = original_client  # type: ignore[misc]

    assert len(drained) == 2
    assert drained[0] == raw_message
    assert len(deleted) == 2
    assert parse_inbound_email(drained[0]).attachments[0].filename == "po.pdf"
