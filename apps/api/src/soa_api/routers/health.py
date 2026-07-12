"""Liveness, readiness, and version endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

import soa_api
from soa_api.dependencies import Dependencies, get_dependencies

router = APIRouter(tags=["health"])


class LivenessResponse(BaseModel):
    status: str


class DependencyStatus(BaseModel):
    name: str
    healthy: bool


class ReadinessResponse(BaseModel):
    status: str
    dependencies: list[DependencyStatus]


class VersionResponse(BaseModel):
    service: str
    version: str
    environment: str


@router.get("/health/live")
async def health_live() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get("/health/ready")
async def health_ready(
    deps: Annotated[Dependencies, Depends(get_dependencies)],
    response: Response,
) -> ReadinessResponse:
    results = await deps.run_readiness_checks()
    all_healthy = all(result.healthy for result in results)
    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if all_healthy else "degraded",
        dependencies=[
            DependencyStatus(name=result.name, healthy=result.healthy) for result in results
        ],
    )


@router.get("/version")
async def version(
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> VersionResponse:
    return VersionResponse(
        service=deps.settings.service_name,
        version=soa_api.__version__,
        environment=deps.settings.environment.value,
    )
