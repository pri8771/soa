"""Email intake tests (ING-014): MIME fixtures, routing, loop
prevention, per-attachment outcomes, safe metadata, webhook security,
Mailpit adapter against a mocked transport."""

from collections.abc import AsyncIterator, Callable
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.app import create_app
from soa_api.dependencies import get_db_session
from soa_api.services import email_intake as email_intake_service
from soa_api.services.email_intake import MailpitPoller, parse_inbound_email, route_recipient
from soa_api.services.malware import NoopScanner, ScanResult
from soa_api.settings import ApiSettings, Environment
from soa_api.test_support.runtime_config import publish_runtime_config
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import Document
from soa_storage import MemoryObjectStore, ObjectMetadata

ADMIN = {"X-Dev-User": "user:reviewer"}
SECRET = "intake-shared-secret-0123456789"
PDF = b"%PDF-1.7 emailed purchase order"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _TransactionCheckingStore(MemoryObjectStore):
    def __init__(self, current_session: Callable[[], AsyncSession]) -> None:
        super().__init__()
        self._current_session = current_session
        self.transaction_states: list[bool] = []
        self.delete_transaction_states: list[bool] = []

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        sha256: str | None = None,
    ) -> ObjectMetadata:
        self.transaction_states.append(self._current_session().in_transaction())
        return await super().put(
            key,
            data,
            content_type=content_type,
            sha256=sha256,
        )

    async def delete(self, key: str) -> None:
        self.delete_transaction_states.append(self._current_session().in_transaction())
        await super().delete(key)


class _TransactionCheckingScanner(NoopScanner):
    def __init__(self, current_session: Callable[[], AsyncSession]) -> None:
        self._current_session = current_session
        self.transaction_states: list[bool] = []

    async def scan(self, data: bytes) -> ScanResult:
        self.transaction_states.append(self._current_session().in_transaction())
        return await super().scan(data)


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
        ApiSettings(environment=Environment.TEST, email_intake_secret=SecretStr(SECRET)),
        db=db,
        object_store=MemoryObjectStore(),
    )
    return TestClient(app, raise_server_exceptions=False), db


async def seed_stream(
    client: TestClient,
    db: DatabaseSessions,
    *,
    stream_overrides: dict[str, object] | None = None,
) -> None:
    for path, body in (
        ("/organizations", {"name": "Northstar", "slug": "northstar"}),
        ("/orgs/northstar/processes", {"name": "POs", "slug": "purchase-orders"}),
        (
            "/orgs/northstar/processes/purchase-orders/streams",
            {"name": "Email intake", "slug": "email"},
        ),
    ):
        assert client.post(path, json=body, headers=ADMIN).status_code == 201
    await publish_runtime_config(client, db, stream_overrides=stream_overrides)


def deliver(client: TestClient, raw: bytes, *, secret: str = SECRET):
    return client.post("/v1/inbound-email", content=raw, headers={"X-Intake-Secret": secret})


async def test_multi_attachment_email_ingests_each_supported_file(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_stream(client, db)
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

    replay = deliver(client, raw)
    assert replay.status_code == 200
    replay_outcomes = {item["filename"]: item for item in replay.json()["attachments"]}
    assert "idempotent replay" in replay_outcomes["po.pdf"]["detail"]
    assert "idempotent replay" in replay_outcomes["scan.png"]["detail"]

    async with db.session_scope() as session:
        documents = (await session.execute(select(Document))).scalars().all()
        assert len(documents) == 2
        for document in documents:
            assert document.source_channel == "email"
            # Safe metadata only: sender/subject/message id — never body.
            assert document.source_metadata["sender"] == "ap@customer.example"
            assert document.source_metadata["subject"] == "PO 4711 attached"
            assert "Body text" not in str(document.source_metadata)


async def test_attachment_scans_and_storage_run_outside_the_sql_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/email-boundary.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    active_session: dict[str, AsyncSession] = {}

    def current_session() -> AsyncSession:
        return active_session["session"]

    store = _TransactionCheckingStore(current_session)
    scanner = _TransactionCheckingScanner(current_session)
    app = create_app(
        ApiSettings(environment=Environment.TEST, email_intake_secret=SecretStr(SECRET)),
        db=db,
        object_store=store,
        malware_scanner=scanner,
    )

    async def tracked_session() -> AsyncIterator[AsyncSession]:
        async with db.session_scope() as session:
            active_session["session"] = session
            yield session

    app.dependency_overrides[get_db_session] = tracked_session
    with TestClient(app, raise_server_exceptions=False) as client:
        await seed_stream(client, db)

        async def fail_finalization(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced registration failure")

        monkeypatch.setattr(
            email_intake_service,
            "finalize_document_intake",
            fail_finalization,
        )
        response = deliver(
            client,
            build_mime(
                attachments=[
                    ("po.pdf", "application/pdf", PDF),
                    ("scan.png", "image/png", PNG),
                ]
            ),
        )

    assert response.status_code == 500
    assert scanner.transaction_states == [False, False]
    assert store.transaction_states == [False, False]
    assert store.delete_transaction_states == [False, False]
    assert await store.list_keys() == []
    async with db.session_scope() as session:
        assert (await session.execute(select(Document))).scalars().all() == []


async def test_email_attachments_honor_the_published_stream_size_limit(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_stream(client, db, stream_overrides={"max_upload_bytes": len(PDF) - 1})
    response = deliver(
        client,
        build_mime(attachments=[("po.pdf", "application/pdf", PDF)]),
    )
    assert response.status_code == 200
    (outcome,) = response.json()["attachments"]
    assert outcome["outcome"] == "rejected"
    assert "byte limit" in outcome["detail"]
    async with db.session_scope() as session:
        assert (await session.execute(select(Document))).scalars().all() == []


async def test_unroutable_empty_and_looping_mail_is_explicit(
    harness: tuple[TestClient, DatabaseSessions],
) -> None:
    client, db = harness
    await seed_stream(client, db)

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
    await seed_stream(client, _db)
    raw = build_mime(attachments=[("po.pdf", "application/pdf", PDF)])
    assert deliver(client, raw, secret="wrong").status_code == 401
    assert client.post("/v1/inbound-email", content=raw).status_code == 401


async def test_email_auth_attempts_are_rate_limited_before_mime_processing(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/limited-auth.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            email_intake_secret=SECRET,
            rate_limit_email_intake_per_minute=1,
        ),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        assert deliver(client, b"not parsed", secret="wrong").status_code == 401
        denied = deliver(client, b"not parsed", secret="wrong")
        assert denied.status_code == 429
        assert denied.headers["Retry-After"] == "60"


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


async def test_webhook_rejects_oversized_raw_mime_before_parsing(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/limited-intake.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            email_intake_secret=SECRET,
            email_intake_max_body_bytes=8,
        ),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = deliver(client, b"x" * 9)
        assert response.status_code == 413


async def test_webhook_rejects_declared_oversize_without_reading_body(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/declared-limit.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            email_intake_secret=SECRET,
            email_intake_max_body_bytes=8,
        ),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/inbound-email",
            content=b"x",
            headers={"X-Intake-Secret": SECRET, "Content-Length": "9"},
        )
        assert response.status_code == 413


async def test_webhook_rejects_attachment_count_and_aggregate_size(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/attachment-limits.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = create_app(
        ApiSettings(
            environment=Environment.TEST,
            email_intake_secret=SECRET,
            email_intake_max_attachments=1,
            email_intake_max_total_attachment_bytes=4,
        ),
        db=DatabaseSessions(engine),
        object_store=MemoryObjectStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        too_many = deliver(
            client,
            build_mime(
                attachments=[
                    ("one.pdf", "application/pdf", b"1"),
                    ("two.pdf", "application/pdf", b"2"),
                ]
            ),
        )
        assert too_many.status_code == 413
        too_large = deliver(
            client,
            build_mime(attachments=[("one.pdf", "application/pdf", b"12345")]),
        )
        assert too_large.status_code == 413


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
