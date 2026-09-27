from datetime import timedelta

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import USER_SESSION_COOKIE, create_identity_token


def authenticate_user(
    client: TestClient,
    *,
    user_id: str,
    settings: Settings,
) -> None:
    """为 API 集成测试建立统一账号登录态。"""

    assert settings.auth_secret is not None
    token = create_identity_token(
        subject=user_id,
        kind="user",
        secret=settings.auth_secret.get_secret_value(),
        lifetime=timedelta(minutes=30),
    )
    client.cookies.set(USER_SESSION_COOKIE, token)
