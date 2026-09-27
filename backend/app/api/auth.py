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
from app.core.security import USER_SESSION_COOKIE
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
    response.delete_cookie(
        USER_SESSION_COOKIE,
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
