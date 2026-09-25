from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import (
    create_session_token,
    get_current_seller,
    get_session_factory_dependency,
    optional_identity_subject,
)
from app.core.config import Settings, get_settings
from app.core.security import BUYER_SESSION_COOKIE, SELLER_SESSION_COOKIE
from app.schemas.auth import (
    BuyerIdentityResponse,
    LogoutResponse,
    SellerIdentityResponse,
    SellerLoginRequest,
)
from app.services.auth_service import AuthService, SellerPrincipal

router = APIRouter(prefix="/auth", tags=["auth"])
SettingsDependency = Annotated[Settings, Depends(get_settings)]
SessionFactory = Annotated[
    sessionmaker[Session],
    Depends(get_session_factory_dependency),
]


@router.post("/visitor", response_model=BuyerIdentityResponse)
def ensure_visitor_identity(
    response: Response,
    settings: SettingsDependency,
    token: Annotated[str | None, Cookie(alias=BUYER_SESSION_COOKIE)] = None,
) -> BuyerIdentityResponse:
    buyer_id = optional_identity_subject(token, kind="buyer", settings=settings)
    if buyer_id is None:
        buyer_id = f"buyer-{uuid4().hex}"

    signed_token, lifetime = create_session_token(
        subject=buyer_id,
        kind="buyer",
        settings=settings,
    )
    expires_at = datetime.now(UTC) + lifetime
    _set_identity_cookie(
        response,
        name=BUYER_SESSION_COOKIE,
        value=signed_token,
        lifetime_seconds=int(lifetime.total_seconds()),
        secure=settings.auth_cookie_secure,
    )
    return BuyerIdentityResponse(buyer_id=buyer_id, expires_at=expires_at)


@router.post("/seller/login", response_model=SellerIdentityResponse)
def seller_login(
    payload: SellerLoginRequest,
    response: Response,
    settings: SettingsDependency,
    session_factory: SessionFactory,
) -> SellerIdentityResponse:
    seller = AuthService(session_factory).authenticate_seller(
        username=payload.username,
        password=payload.password,
    )
    if seller is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        )

    signed_token, lifetime = create_session_token(
        subject=seller.id,
        kind="seller",
        settings=settings,
    )
    expires_at = datetime.now(UTC) + lifetime
    _set_identity_cookie(
        response,
        name=SELLER_SESSION_COOKIE,
        value=signed_token,
        lifetime_seconds=int(lifetime.total_seconds()),
        secure=settings.auth_cookie_secure,
    )
    return SellerIdentityResponse(
        id=seller.id,
        username=seller.username,
        expires_at=expires_at,
    )


@router.get("/seller/me", response_model=SellerIdentityResponse)
def seller_me(
    seller: Annotated[SellerPrincipal, Depends(get_current_seller)],
) -> SellerIdentityResponse:
    return SellerIdentityResponse(
        id=seller.id,
        username=seller.username,
    )


@router.post("/seller/logout", response_model=LogoutResponse)
def seller_logout(response: Response) -> LogoutResponse:
    response.delete_cookie(
        SELLER_SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return LogoutResponse()


def _set_identity_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    lifetime_seconds: int,
    secure: bool,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=lifetime_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
