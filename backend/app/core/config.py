from decimal import Decimal
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
        populate_by_name=True,
    )

    app_name: str = "SecondHand Agent API"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api"
    cors_origins: list[str] = ["http://localhost:5173"]

    database_url: str

    # 身份令牌没有源码内默认密钥；未配置时认证接口会明确拒绝服务。
    auth_secret: SecretStr | None = None
    auth_cookie_secure: bool = False
    user_session_minutes: int = Field(default=480, ge=5, le=10080)
    demo_seller_password: SecretStr | None = None

    model_provider: str = "qwen"
    model_name: str = "qwen-plus"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_enable_thinking: bool = False
    model_thinking_budget: int = Field(default=768, ge=1, le=32768)
    model_timeout_seconds: float = Field(default=30.0, gt=0.0)
    model_max_retries: int = Field(default=2, ge=0, le=10)
    model_task_max_attempts: int = Field(default=3, ge=1, le=20)
    model_task_retry_base_seconds: float = Field(default=2.0, gt=0.0, le=3600.0)
    model_task_retry_max_seconds: float = Field(default=300.0, gt=0.0, le=86400.0)
    model_task_retry_jitter_ratio: float = Field(default=0.2, ge=0.0, le=0.5)
    model_task_lease_seconds: int = Field(default=300, ge=1, le=3600)
    model_input_price_per_million: Decimal | None = Field(default=None, ge=0)
    model_output_price_per_million: Decimal | None = Field(default=None, ge=0)
    model_cached_input_price_per_million: Decimal | None = Field(default=None, ge=0)
    model_cost_currency: Literal["CNY", "USD"] | None = None

    agent_runtime: Literal["V3", "V4"] = "V3"
    agent_reply_strategy: Literal["legacy-v1", "conversation-v1"] = "conversation-v1"
    agent_reply_review_enable_thinking: bool | None = None
    agent_v4_session_ids: list[int] | None = None
    agent_context_window: int = Field(default=32768, ge=4096)
    agent_input_token_budget: int = Field(default=16000, ge=1024)
    agent_output_token_reserve: int = Field(default=1024, ge=128)
    agent_token_safety_margin: int = Field(default=1024, ge=128)
    agent_initial_tool_growth_reserve: int = Field(default=2048, ge=0)
    agent_compression_trigger_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)
    agent_compression_target_ratio: float = Field(default=0.6, gt=0.0, lt=0.8)
    agent_tool_result_token_limit: int = Field(default=1500, ge=128)
    agent_max_model_steps: int = Field(default=6, ge=1, le=30)
    agent_max_total_model_calls: int = Field(default=10, ge=1, le=100)
    agent_max_maintenance_calls: int = Field(default=2, ge=0, le=10)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=100)
    agent_max_wall_seconds: float = Field(default=120, gt=0, le=600)
    agent_max_total_tokens: int = Field(default=180000, ge=1024)
    agent_max_estimated_cost: Decimal | None = Field(default=None, gt=0)
    agent_memory_enabled: bool = True

    observability_langsmith_enabled: bool = False
    observability_langsmith_api_key: SecretStr | None = None
    observability_langsmith_endpoint: str = "https://api.smith.langchain.com"
    observability_langsmith_project: str = "secondhand-agent"
    observability_langsmith_timeout_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=30.0,
    )

    @property
    def model_is_configured(self) -> bool:
        """只报告必要配置是否存在，不访问或回显密钥。"""

        if self.model_api_key is None or not self.model_base_url:
            return False
        return bool(self.model_api_key.get_secret_value().strip() and self.model_base_url.strip())

    @property
    def auth_is_configured(self) -> bool:
        """认证签名密钥至少需要 32 个字符，避免弱密钥进入运行环境。"""

        if self.auth_secret is None:
            return False
        value = self.auth_secret.get_secret_value().strip()
        return len(value) >= 32 and "CHANGE_ME" not in value.upper()

    @property
    def model_cost_is_configured(self) -> bool:
        return (
            self.model_input_price_per_million is not None
            and self.model_output_price_per_million is not None
            and self.model_cost_currency is not None
        )

    @property
    def langsmith_is_configured(self) -> bool:
        if not self.observability_langsmith_enabled:
            return False
        if self.observability_langsmith_api_key is None:
            return False
        return bool(self.observability_langsmith_api_key.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
