"""Application dependency container and readiness registry.

Later tasks register real dependencies (database, object store, queue) as
readiness checks. The container is constructed once per application and
stored on ``app.state`` so request handlers resolve shared resources without
module-level globals.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from soa_api.services.malware import MalwareScanner
from soa_api.services.rate_limit import RateLimiter, SlidingWindowRateLimiter
from soa_api.settings import ApiSettings
from soa_config import SecretStore
from soa_config.telemetry import Telemetry
from soa_db import DatabaseSessions
from soa_storage import ObjectStore

if TYPE_CHECKING:
    from soa_api.auth.oidc import OidcTokenValidator

ReadinessCheck = Callable[[], Awaitable[bool]]


@dataclass
class ReadinessResult:
    name: str
    healthy: bool


@dataclass
class Dependencies:
    settings: ApiSettings
    telemetry: Telemetry = field(default_factory=Telemetry.noop)
    oidc_validator: "OidcTokenValidator | None" = None
    db: DatabaseSessions | None = None
    object_store: ObjectStore | None = None
    malware_scanner: MalwareScanner | None = None
    secret_store: SecretStore | None = None
    rate_limiter: RateLimiter = field(default_factory=SlidingWindowRateLimiter)
    _readiness_checks: dict[str, ReadinessCheck] = field(default_factory=dict)

    def register_readiness_check(self, name: str, check: ReadinessCheck) -> None:
        if name in self._readiness_checks:
            raise ValueError(f"readiness check {name!r} is already registered")
        self._readiness_checks[name] = check

    async def run_readiness_checks(self) -> list[ReadinessResult]:
        async def execute(name: str, check: ReadinessCheck) -> ReadinessResult:
            try:
                healthy = await check()
            except Exception:
                healthy = False
            return ReadinessResult(name=name, healthy=healthy)

        # Dependency probes are independent; the slowest dependency should
        # bound readiness latency, not the sum of all network timeouts.
        return list(
            await asyncio.gather(
                *(execute(name, check) for name, check in self._readiness_checks.items())
            )
        )


def get_dependencies(request: Request) -> Dependencies:
    deps = request.app.state.dependencies
    assert isinstance(deps, Dependencies)
    return deps


async def get_db_session(
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> AsyncIterator[AsyncSession]:
    """Session-per-request unit of work: commits on success, rolls back on
    error, stays open for the whole request."""
    if deps.db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not configured.",
        )
    async with deps.db.session_scope() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def get_settings(deps: Annotated[Dependencies, Depends(get_dependencies)]) -> ApiSettings:
    return deps.settings


SettingsDep = Annotated[ApiSettings, Depends(get_settings)]


def get_object_store(
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> ObjectStore:
    if deps.object_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured.",
        )
    return deps.object_store


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]


def get_malware_scanner(
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> MalwareScanner:
    if deps.malware_scanner is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning is not configured.",
        )
    return deps.malware_scanner


MalwareScannerDep = Annotated[MalwareScanner, Depends(get_malware_scanner)]


def get_secret_store(
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> SecretStore:
    if deps.secret_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The secret store is not configured.",
        )
    return deps.secret_store


SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]
