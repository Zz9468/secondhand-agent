from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "SecondHand Agent API"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api"
    cors_origins: list[str] = ["http://localhost:5173"]

    database_url: str = (
        "mysql+pymysql://secondhand:secondhand_dev_password@127.0.0.1:3306/"
        "secondhand_agent?charset=utf8mb4"
    )

    model_provider: str = "qwen"
    model_name: str = "qwen-plus"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_timeout_seconds: float = Field(default=30.0, gt=0.0)
    model_max_retries: int = Field(default=2, ge=0, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()
