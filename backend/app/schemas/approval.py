from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import ApprovalFollowupStatus, ApprovalStatus


class CreateApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    offer_id: int = Field(gt=0)
    expected_policy_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: datetime

    @field_validator("reason")
    @classmethod
    def reject_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("审批原因不能为空")
        return value.strip()


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    offer_id: int
    policy_version: int
    status: ApprovalStatus
    reason: str
    seller_comment: str | None
    expires_at: datetime
    reviewed_at: datetime | None
    followup_status: ApprovalFollowupStatus | None
    followup_request_id: str | None
    created_at: datetime
    updated_at: datetime


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalResponse]
