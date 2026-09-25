from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["ok"]
    model: Literal["configured", "not_configured"]
    authentication: Literal["configured", "not_configured"]
