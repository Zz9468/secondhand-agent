import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少数据库连接配置时应立即失败，不能回退到固定口令。"""

    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_auth_secret_requires_explicit_strong_value() -> None:
    missing = Settings(_env_file=None, database_url="sqlite://")
    weak = Settings(
        _env_file=None,
        database_url="sqlite://",
        auth_secret="too-short",
    )
    placeholder = Settings(
        _env_file=None,
        database_url="sqlite://",
        auth_secret="CHANGE_ME_TO_A_RANDOM_SECRET_AT_LEAST_32_CHARS",
    )
    configured = Settings(
        _env_file=None,
        database_url="sqlite://",
        auth_secret="a-secure-test-secret-with-32-characters",
    )

    assert missing.auth_is_configured is False
    assert weak.auth_is_configured is False
    assert placeholder.auth_is_configured is False
    assert configured.auth_is_configured is True


def test_unified_session_lifetime_ignores_removed_seller_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USER_SESSION_MINUTES", raising=False)
    monkeypatch.setenv("SELLER_SESSION_MINUTES", "720")

    without_unified_setting = Settings(_env_file=None, database_url="sqlite://")
    explicit = Settings(
        _env_file=None,
        database_url="sqlite://",
        user_session_minutes=60,
    )

    assert without_unified_setting.user_session_minutes == 480
    assert explicit.user_session_minutes == 60
