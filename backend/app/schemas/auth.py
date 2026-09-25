from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SellerLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.lower()


class SellerIdentityResponse(BaseModel):
    id: str
    username: str
    expires_at: datetime | None = None


class BuyerIdentityResponse(BaseModel):
    buyer_id: str
    expires_at: datetime


class LogoutResponse(BaseModel):
    logged_out: bool = True
