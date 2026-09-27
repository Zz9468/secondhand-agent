import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import (
    USER_SESSION_COOKIE,
    hash_password,
    verify_password,
)
from app.db.models import UserAccount
from app.main import create_app
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration

TEST_SETTINGS = Settings(
    _env_file=None,
    database_url="mysql+pymysql://test:test@127.0.0.1/test",
    auth_secret="a-secure-test-secret-with-32-characters",
)


def _test_client(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings = TEST_SETTINGS,
) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: session_factory
    )
    application.dependency_overrides[get_settings] = lambda: settings
    return TestClient(application)


def _add_user(
    session_factory: sessionmaker[Session],
    *,
    is_active: bool = True,
) -> tuple[str, str, str]:
    suffix = uuid4().hex
    user_id = f"user-{suffix}"
    username = f"account-{suffix}"
    password = "account-test-password"
    with session_factory() as db, db.begin():
        db.add(
            UserAccount(
                id=user_id,
                username=username,
                display_name="认证测试账号",
                password_hash=hash_password(password),
                is_active=is_active,
            )
        )
    return user_id, username, password


def _legacy_token(*, subject: str, kind: str) -> str:
    """仅为回归测试构造旧类型令牌，生产代码不再提供签发入口。"""

    assert TEST_SETTINGS.auth_secret is not None
    secret = TEST_SETTINGS.auth_secret.get_secret_value()
    now = datetime.now(UTC)
    payload = {
        "exp": int((now + timedelta(minutes=30)).timestamp()),
        "iat": int(now.timestamp()),
        "sub": subject,
        "typ": kind,
        "ver": 1,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        secret.encode(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded}.{encoded_signature}"


def test_register_me_duplicate_and_logout(
    service_session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid4().hex
    username = f"member-{suffix}"
    password = "Strong-account-password-2026"
    client = _test_client(service_session_factory)

    registered = client.post(
        "/api/auth/register",
        json={
            "username": username.upper(),
            "display_name": "  测试用户  ",
            "password": password,
        },
    )
    profile = client.get("/api/auth/me")
    duplicate = _test_client(service_session_factory).post(
        "/api/auth/register",
        json={
            "username": username,
            "display_name": "重复账号",
            "password": password,
        },
    )
    logged_out = client.post("/api/auth/logout")
    profile_after_logout = client.get("/api/auth/me")

    assert registered.status_code == 201
    assert registered.json()["username"] == username
    assert registered.json()["display_name"] == "测试用户"
    assert registered.json()["expires_at"] is not None
    assert password not in registered.text
    assert "password_hash" not in registered.text
    cookie_header = registered.headers["set-cookie"]
    assert USER_SESSION_COOKIE in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert "secondhand_seller_session" not in cookie_header
    assert "secondhand_buyer_session" not in cookie_header
    assert profile.status_code == 200
    assert profile.json()["id"] == registered.json()["id"]
    assert profile.json()["expires_at"] is None
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "用户名已被使用"}
    assert "password" not in duplicate.text.lower()
    assert logged_out.status_code == 200
    assert profile_after_logout.status_code == 401

    with service_session_factory() as db:
        account = db.scalar(select(UserAccount).where(UserAccount.username == username))
        assert account is not None
        assert account.password_hash != password
        assert verify_password(password, account.password_hash) is True


def test_login_rejects_wrong_password_and_disabled_account(
    service_session_factory: sessionmaker[Session],
) -> None:
    user_id, username, password = _add_user(service_session_factory)
    _, disabled_username, disabled_password = _add_user(
        service_session_factory,
        is_active=False,
    )
    client = _test_client(service_session_factory)

    rejected = client.post(
        "/api/auth/login",
        json={"username": username, "password": "wrong-test-password"},
    )
    disabled = client.post(
        "/api/auth/login",
        json={"username": disabled_username, "password": disabled_password},
    )
    logged_in = client.post(
        "/api/auth/login",
        json={"username": username.upper(), "password": password},
    )
    with service_session_factory() as db, db.begin():
        account = db.get(UserAccount, user_id)
        assert account is not None
        account.is_active = False
    disabled_session = client.get("/api/auth/me")

    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "invalid username or password"}
    assert disabled.status_code == 401
    assert disabled.json() == rejected.json()
    assert logged_in.status_code == 200
    assert logged_in.json()["id"] == user_id
    assert logged_in.json()["display_name"] == "认证测试账号"
    assert disabled_session.status_code == 401
    assert disabled_session.json() == {"detail": "user session is no longer valid"}


def test_same_login_can_access_buyer_and_seller_resources(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    with service_session_factory() as db:
        buyer = db.get(UserAccount, buyer_id)
        assert buyer is not None
        username = buyer.username

    client = _test_client(service_session_factory)
    logged_in = client.post(
        "/api/auth/login",
        json={"username": username, "password": "integration-test-password"},
    )
    buyer_resource = client.get(f"/api/negotiations/{session_id}")
    seller_resource = client.post(
        "/api/products",
        json={
            "title": "统一账号发布的商品",
            "description": "验证同一登录态可进入买卖两侧。",
            "listed_price": "100.00",
            "status": "DRAFT",
            "policy": {
                "minimum_net_price": "80.00",
                "auto_accept_threshold": "90.00",
                "negotiation_style": "BALANCED",
                "max_rounds": 4,
            },
        },
    )

    assert logged_in.status_code == 200
    assert logged_in.json()["id"] == buyer_id
    assert buyer_resource.status_code == 200
    assert seller_resource.status_code == 201


def test_legacy_cookies_and_token_kinds_are_not_authorization_sources(
    service_session_factory: sessionmaker[Session],
) -> None:
    user_id, _, _ = _add_user(service_session_factory)

    legacy_seller = _test_client(service_session_factory)
    legacy_seller.cookies.set(
        "secondhand_seller_session",
        _legacy_token(subject=user_id, kind="seller"),
    )
    legacy_buyer = _test_client(service_session_factory)
    legacy_buyer.cookies.set(
        "secondhand_buyer_session",
        _legacy_token(subject=user_id, kind="buyer"),
    )
    wrong_kind = _test_client(service_session_factory)
    wrong_kind.cookies.set(
        USER_SESSION_COOKIE,
        _legacy_token(subject=user_id, kind="seller"),
    )

    assert legacy_seller.get("/api/seller/products").status_code == 401
    assert legacy_buyer.get("/api/seller/products").status_code == 401
    assert wrong_kind.get("/api/seller/products").status_code == 401


def test_removed_authentication_routes_return_not_found(
    service_session_factory: sessionmaker[Session],
) -> None:
    client = _test_client(service_session_factory)

    visitor = client.post("/api/auth/visitor")
    logged_in = client.post(
        "/api/auth/seller/login",
        json={"username": "removed", "password": "removed-password"},
    )
    profile = client.get("/api/auth/seller/me")
    logout = client.post("/api/auth/seller/logout")

    assert visitor.status_code == 404
    assert "set-cookie" not in visitor.headers
    assert logged_in.status_code == 404
    assert profile.status_code == 404
    assert logout.status_code == 404


def test_authentication_endpoints_fail_closed_without_secret(
    service_session_factory: sessionmaker[Session],
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="mysql+pymysql://test:test@127.0.0.1/test",
    )
    username = f"no-secret-{uuid4().hex}"
    client = _test_client(service_session_factory, settings=settings)

    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "display_name": "不应写入",
            "password": "Strong-account-password-2026",
        },
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "authentication service is not configured"
    }
    with service_session_factory() as db:
        assert (
            db.scalar(select(UserAccount).where(UserAccount.username == username))
            is None
        )
