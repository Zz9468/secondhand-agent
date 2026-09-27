import unicodedata
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class UserRegisterRequest(UserLoginRequest):
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFKC", value).strip()
        if any(
            unicodedata.category(character).startswith("C")
            or (character.isspace() and character != " ")
            for character in normalized
        ):
            raise ValueError("公开显示名称不能包含控制字符")
        return normalized

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        categories = (
            any(character.isalpha() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() and not character.isspace() for character in value),
        )
        if sum(categories) < 2:
            raise ValueError("密码至少应包含字母、数字、符号中的两类")
        return value


class UserIdentityResponse(BaseModel):
    id: str
    username: str
    display_name: str
    expires_at: datetime | None = None


class LogoutResponse(BaseModel):
    logged_out: bool = True
