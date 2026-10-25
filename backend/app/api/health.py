from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.db.session import check_database_connection
from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    response_model_exclude_none=True,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Database unavailable"}},
)
def readiness(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReadinessResponse:
    try:
        check_database_connection()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc

    model_status = "configured" if settings.model_is_configured else "not_configured"
    auth_status = "configured" if settings.auth_is_configured else "not_configured"
    components = {}
    if settings.graph_require_services:
        from app.runtime.readiness import graph_readiness

        components = graph_readiness(settings)
    result = ReadinessResponse(
        status=(
            "ready"
            if settings.model_is_configured
            and settings.auth_is_configured
            and all(value == "ok" for value in components.values())
            else "degraded"
        ),
        database="ok",
        model=model_status,
        authentication=auth_status,
        **components,
    )
    if settings.graph_require_services and result.status != "ready":
        return JSONResponse(status_code=503, content=result.model_dump(exclude_none=True))
    return result
