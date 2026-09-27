from datetime import UTC, datetime
from typing import Annotated, Never

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import (
    CurrentUser,
    create_user_session_token,
    ensure_auth_configured,
    get_session_factory_dependency,
)
from app.core.config import Settings, get_settings
from app.core.security import (
    BUYER_SESSION_COOKIE,
    SELLER_SESSION_COOKIE,
    USER_SESSION_COOKIE,
)
from app.schemas.auth import (
    LogoutResponse,
    UserIdentityResponse,
    UserLoginRequest,
    UserRegisterRequest,
)
from app.services.auth_service import AuthService, UserPrincipal
from app.services.errors import UsernameAlreadyExistsError

router = APIRouter(prefix="/auth", tags=["auth"])
SettingsDependency = Annotated[Settings, Depends(get_settings)]
SessionFactory = Annotated[
    sessionmaker[Session],
    Depends(get_session_factory_dependency),
]


@router.post(
    "/register",
    response_model=UserIdentityResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: UserRegisterRequest,
    response: Response,
    settings: SettingsDependency,
    session_factory: SessionFactory,
) -> UserIdentityResponse:
    ensure_auth_configured(settings)
    try:
        user = AuthService(session_factory).register_user(
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
        )
    except UsernameAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return _establish_login(user, response=response, settings=settings)


@router.post("/login", response_model=UserIdentityResponse)
def login(
    payload: UserLoginRequest,
    response: Response,
    settings: SettingsDependency,
    session_factory: SessionFactory,
) -> UserIdentityResponse:
    ensure_auth_configured(settings)
    user = AuthService(session_factory).authenticate_user(
        username=payload.username,
        password=payload.password,
    )
    if user is None:
        _raise_invalid_credentials()
    return _establish_login(user, response=response, settings=settings)


@router.get("/me", response_model=UserIdentityResponse)
def me(user: CurrentUser) -> UserIdentityResponse:
    return _identity_response(user)


@router.post("/logout", response_model=LogoutResponse)
def logout(
    response: Response,
    settings: SettingsDependency,
) -> LogoutResponse:
    _delete_identity_cookies(response, secure=settings.auth_cookie_secure)
    return LogoutResponse()


@router.post("/visitor", deprecated=True, response_model=None)
def disabled_visitor_identity() -> None:
    """阶段七删除路由前，明确阻止旧访客入口继续签发身份。"""

    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="visitor authentication has been replaced by unified account login",
    )


@router.post(
    "/seller/login",
    response_model=UserIdentityResponse,
    deprecated=True,
)
def legacy_seller_login(
    payload: UserLoginRequest,
    response: Response,
    settings: SettingsDependency,
    session_factory: SessionFactory,
) -> UserIdentityResponse:
    """兼容尚未迁移的卖家页面，但只建立统一账号登录态。"""

    return login(payload, response, settings, session_factory)


@router.get(
    "/seller/me",
    response_model=UserIdentityResponse,
    deprecated=True,
)
def legacy_seller_me(user: CurrentUser) -> UserIdentityResponse:
    return _identity_response(user)


@router.post(
    "/seller/logout",
    response_model=LogoutResponse,
    deprecated=True,
)
def legacy_seller_logout(
    response: Response,
    settings: SettingsDependency,
) -> LogoutResponse:
    return logout(response, settings)


def _establish_login(
    user: UserPrincipal,
    *,
    response: Response,
    settings: Settings,
) -> UserIdentityResponse:
    signed_token, lifetime = create_user_session_token(
        subject=user.id,
        settings=settings,
    )
    expires_at = datetime.now(UTC) + lifetime
    response.set_cookie(
        key=USER_SESSION_COOKIE,
        value=signed_token,
        max_age=int(lifetime.total_seconds()),
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return _identity_response(user, expires_at=expires_at)


def _identity_response(
    user: UserPrincipal,
    *,
    expires_at: datetime | None = None,
) -> UserIdentityResponse:
    return UserIdentityResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        expires_at=expires_at,
    )


def _delete_identity_cookies(response: Response, *, secure: bool) -> None:
    for cookie_name in (
        USER_SESSION_COOKIE,
        SELLER_SESSION_COOKIE,
        BUYER_SESSION_COOKIE,
    ):
        response.delete_cookie(
            cookie_name,
            path="/",
            httponly=True,
            secure=secure,
            samesite="lax",
        )


def _raise_invalid_credentials() -> Never:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid username or password",
    )
