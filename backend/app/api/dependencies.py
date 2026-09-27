from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import DecisionProvider, LangChainDecisionProvider
from app.agent.model_factory import ModelConfigurationError, QwenChatModelFactory
from app.core.config import Settings, get_settings
from app.core.security import (
    USER_SESSION_COOKIE,
    IdentityTokenError,
    create_identity_token,
    decode_identity_token,
)
from app.db.session import get_session_factory
from app.services.auth_service import AuthService, UserPrincipal


def get_session_factory_dependency() -> sessionmaker[Session]:
    return get_session_factory()


SettingsDependency = Annotated[Settings, Depends(get_settings)]
SessionFactoryDependency = Annotated[
    sessionmaker[Session],
    Depends(get_session_factory_dependency),
]


@lru_cache
def _get_configured_decision_provider() -> DecisionProvider:
    model = QwenChatModelFactory().create(get_settings())
    return LangChainDecisionProvider(model)


def get_decision_provider() -> DecisionProvider:
    try:
        return _get_configured_decision_provider()
    except ModelConfigurationError as exc:
        # 对外只说明配置缺失，不回显可能包含服务地址的配置细节。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="model service is not configured",
        ) from exc


def get_current_user(
    settings: SettingsDependency,
    session_factory: SessionFactoryDependency,
    token: Annotated[str | None, Cookie(alias=USER_SESSION_COOKIE)] = None,
) -> UserPrincipal:
    user_id = _required_user_subject(token, settings=settings)
    user = AuthService(session_factory).get_active_user(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user session is no longer valid",
        )
    return user


CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


def create_user_session_token(
    *,
    subject: str,
    settings: Settings,
) -> tuple[str, timedelta]:
    secret = _auth_secret(settings)
    lifetime = timedelta(minutes=settings.user_session_minutes)
    return (
        create_identity_token(
            subject=subject,
            secret=secret,
            lifetime=lifetime,
        ),
        lifetime,
    )


def ensure_auth_configured(settings: Settings) -> None:
    """在写入账号或校验凭据前确认签名服务可用。"""

    _auth_secret(settings)


def _required_user_subject(
    token: str | None,
    *,
    settings: Settings,
) -> str:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user session is required",
        )
    try:
        return decode_identity_token(
            token,
            secret=_auth_secret(settings),
        ).subject
    except IdentityTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user session is invalid or expired",
        ) from exc


def _auth_secret(settings: Settings) -> str:
    if not settings.auth_is_configured or settings.auth_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service is not configured",
        )
    return settings.auth_secret.get_secret_value().strip()
