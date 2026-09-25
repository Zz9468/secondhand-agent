from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

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
    return ReadinessResponse(
        status="ready" if settings.model_is_configured else "degraded",
        database="ok",
        model=model_status,
    )
