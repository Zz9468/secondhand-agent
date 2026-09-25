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

    database_url: str

    model_provider: str = "qwen"
    model_name: str = "qwen-plus"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_timeout_seconds: float = Field(default=30.0, gt=0.0)
    model_max_retries: int = Field(default=2, ge=0, le=10)

    @property
    def model_is_configured(self) -> bool:
        """只报告必要配置是否存在，不访问或回显密钥。"""

        if self.model_api_key is None or not self.model_base_url:
            return False
        return bool(
            self.model_api_key.get_secret_value().strip()
            and self.model_base_url.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
