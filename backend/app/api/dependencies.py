from datetime import timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session, sessionmaker

from app.agent.decision_provider import DecisionProvider, LangChainDecisionProvider
from app.agent.model_factory import ModelConfigurationError, QwenChatModelFactory
from app.core.config import Settings, get_settings
from app.core.security import (
    BUYER_SESSION_COOKIE,
    SELLER_SESSION_COOKIE,
    IdentityKind,
    IdentityTokenError,
    create_identity_token,
    decode_identity_token,
)
from app.db.session import get_session_factory
from app.services.auth_service import AuthService, SellerPrincipal


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


def get_current_buyer_id(
    settings: SettingsDependency,
    token: Annotated[str | None, Cookie(alias=BUYER_SESSION_COOKIE)] = None,
) -> str:
    return _required_identity_subject(token, kind="buyer", settings=settings)


def get_current_seller(
    settings: SettingsDependency,
    session_factory: SessionFactoryDependency,
    token: Annotated[str | None, Cookie(alias=SELLER_SESSION_COOKIE)] = None,
) -> SellerPrincipal:
    seller_id = _required_identity_subject(token, kind="seller", settings=settings)
    seller = AuthService(session_factory).get_active_seller(seller_id)
    if seller is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="seller session is no longer valid",
        )
    return seller


def create_session_token(
    *,
    subject: str,
    kind: IdentityKind,
    settings: Settings,
) -> tuple[str, timedelta]:
    secret = _auth_secret(settings)
    lifetime = (
        timedelta(minutes=settings.seller_session_minutes)
        if kind == "seller"
        else timedelta(days=settings.buyer_session_days)
    )
    return (
        create_identity_token(
            subject=subject,
            kind=kind,
            secret=secret,
            lifetime=lifetime,
        ),
        lifetime,
    )


def optional_identity_subject(
    token: str | None,
    *,
    kind: IdentityKind,
    settings: Settings,
) -> str | None:
    if not token:
        return None
    try:
        return decode_identity_token(
            token,
            expected_kind=kind,
            secret=_auth_secret(settings),
        ).subject
    except IdentityTokenError:
        return None


def _required_identity_subject(
    token: str | None,
    *,
    kind: IdentityKind,
    settings: Settings,
) -> str:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{kind} session is required",
        )
    try:
        return decode_identity_token(
            token,
            expected_kind=kind,
            secret=_auth_secret(settings),
        ).subject
    except IdentityTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{kind} session is invalid or expired",
        ) from exc


def _auth_secret(settings: Settings) -> str:
    if not settings.auth_is_configured or settings.auth_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication service is not configured",
        )
    return settings.auth_secret.get_secret_value().strip()
