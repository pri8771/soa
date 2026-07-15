import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_api.auth.errors import AuthenticationError
from soa_api.auth.principal import AuthMethod
from soa_api.domain.credentials import (
    ScopeError,
    authenticate_api_key,
    create_credential,
    credential_organization_id,
    require_scope,
    revoke_credential,
    rotate_credential,
)
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.repository import OrganizationContext

ORG = OrganizationContext(organization_id=uuid.UUID(int=0xE1))


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/creds.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def make_key(db: DatabaseSessions, **kwargs: object) -> str:
    async with db.session_scope() as session:
        _, raw_key = await create_credential(
            session,
            ORG,
            name="ERP intake",
            scopes=["documents.upload"],
            actor_id="user:admin",
            **kwargs,  # type: ignore[arg-type]
        )
    return raw_key


async def test_create_returns_raw_key_once_and_stores_only_hash(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        credential, raw_key = await create_credential(
            session,
            ORG,
            name="ERP intake",
            scopes=["documents.upload"],
            actor_id="user:admin",
        )
        assert raw_key.startswith("soa_")
        assert raw_key not in credential.key_hash
        assert credential.key_hash != raw_key
        assert len(credential.key_hash) == 64  # sha256 hex
        assert raw_key not in repr(credential)
    async with db.session_scope() as session:
        events = (await session.execute(select(AuditEvent))).scalars().all()
        assert events[0].action == "credential.created"
        assert raw_key not in str(events[0].summary)
    await db.dispose()


async def test_valid_key_authenticates_with_scopes_and_org(db: DatabaseSessions) -> None:
    raw_key = await make_key(db)
    async with db.session_scope() as session:
        principal = await authenticate_api_key(session, raw_key)
    assert principal.auth_method is AuthMethod.API_KEY
    assert credential_organization_id(principal) == ORG.organization_id
    require_scope(principal, "documents.upload")  # must not raise
    await db.dispose()


async def test_wrong_secret_with_valid_prefix_fails(db: DatabaseSessions) -> None:
    raw_key = await make_key(db)
    prefix = raw_key.split("_")[1]
    forged = f"soa_{prefix}_forged-secret-material-0123456789"
    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError, match="Invalid API key"):
            await authenticate_api_key(session, forged)
    await db.dispose()


@pytest.mark.parametrize(
    "bad_key",
    ["", "not-a-key", "soa_short_x", "other_abcd1234_secret", "soa_deadbeef_unknownprefix"],
)
async def test_malformed_and_unknown_keys_fail_identically(
    db: DatabaseSessions, bad_key: str
) -> None:
    await make_key(db)
    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError, match="Invalid API key"):
            await authenticate_api_key(session, bad_key)
    await db.dispose()


async def test_revoked_key_fails_closed(db: DatabaseSessions) -> None:
    raw_key = await make_key(db)
    async with db.session_scope() as session:
        principal = await authenticate_api_key(session, raw_key)
        credential_id = uuid.UUID(principal.subject)
        from soa_api.domain.credentials import ServiceCredentialRepository

        credential = await ServiceCredentialRepository(session, ORG).get(credential_id)
        assert credential is not None
        await revoke_credential(session, ORG, credential=credential, actor_id="user:admin")
    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError):
            await authenticate_api_key(session, raw_key)
    await db.dispose()


async def test_expired_key_fails_closed(db: DatabaseSessions) -> None:
    raw_key = await make_key(db, expires_in=timedelta(seconds=-1))
    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError):
            await authenticate_api_key(session, raw_key)
    await db.dispose()


async def test_rotation_invalidates_old_key(db: DatabaseSessions) -> None:
    raw_key = await make_key(db)
    async with db.session_scope() as session:
        principal = await authenticate_api_key(session, raw_key)
        from soa_api.domain.credentials import ServiceCredentialRepository

        credential = await ServiceCredentialRepository(session, ORG).get(
            uuid.UUID(principal.subject)
        )
        assert credential is not None
        new_key = await rotate_credential(session, ORG, credential=credential, actor_id="u:a")
    async with db.session_scope() as session:
        with pytest.raises(AuthenticationError):
            await authenticate_api_key(session, raw_key)
        rotated = await authenticate_api_key(session, new_key)
    assert rotated.display_name == "ERP intake"
    await db.dispose()


async def test_unknown_scope_fails_closed(db: DatabaseSessions) -> None:
    raw_key = await make_key(db)
    async with db.session_scope() as session:
        principal = await authenticate_api_key(session, raw_key)
    from soa_api.domain.rbac import UnknownPermissionError

    with pytest.raises(UnknownPermissionError):
        require_scope(principal, "documents.obliterate")
    with pytest.raises(ScopeError):
        require_scope(principal, "documents.approve")  # registered but not granted
    await db.dispose()


async def test_create_with_unregistered_scope_is_rejected(db: DatabaseSessions) -> None:
    from soa_api.domain.rbac import UnknownPermissionError

    with pytest.raises(UnknownPermissionError):
        async with db.session_scope() as session:
            await create_credential(
                session, ORG, name="bad", scopes=["everything.always"], actor_id="u:a"
            )
    await db.dispose()


async def test_human_only_permission_cannot_be_minted_into_service_key(
    db: DatabaseSessions,
) -> None:
    with pytest.raises(ScopeError, match="unsupported service-credential scopes"):
        async with db.session_scope() as session:
            await create_credential(
                session,
                ORG,
                name="overprivileged",
                scopes=["credentials.manage"],
                actor_id="u:a",
            )
    await db.dispose()
