from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import ModelTaskErrorCategory, ModelTaskStatus, ModelTaskType


class SellerModelTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_type: ModelTaskType
    status: ModelTaskStatus
    session_id: int
    product_id: int
    product_title: str
    buyer_display_name: str
    offer_id: int | None
    approval_id: int | None
    attempt_count: int
    max_attempts: int
    manual_retry_count: int
    next_retry_at: datetime | None
    lease_expires_at: datetime | None
    last_error_category: ModelTaskErrorCategory | None
    last_error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    last_manual_action: str | None
    last_manual_actor_id: str | None
    last_manual_reason: str | None
    last_manual_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SellerModelTaskListResponse(BaseModel):
    tasks: list[SellerModelTaskResponse]


class ModelTaskManualActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = Field(default=None, max_length=300)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None
