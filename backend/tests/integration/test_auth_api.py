from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.models import SellerAccount
from app.main import create_app

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


def test_seller_login_me_and_logout(
    service_session_factory: sessionmaker[Session],
) -> None:
    suffix = uuid4().hex
    username = f"seller-{suffix}"
    password = "seller-test-password"
    with service_session_factory() as db, db.begin():
        db.add(
            SellerAccount(
                id=username,
                username=username,
                password_hash=hash_password(password),
                is_active=True,
            )
        )

    client = _test_client(service_session_factory)
    rejected = client.post(
        "/api/auth/seller/login",
        json={"username": username, "password": "wrong-test-password"},
    )
    logged_in = client.post(
        "/api/auth/seller/login",
        json={"username": username.upper(), "password": password},
    )
    profile = client.get("/api/auth/seller/me")
    logged_out = client.post("/api/auth/seller/logout")
    profile_after_logout = client.get("/api/auth/seller/me")

    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "invalid username or password"}
    assert logged_in.status_code == 200
    assert logged_in.json()["id"] == username
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    assert "SameSite=lax" in logged_in.headers["set-cookie"]
    assert profile.status_code == 200
    assert profile.json()["username"] == username
    assert logged_out.status_code == 200
    assert profile_after_logout.status_code == 401


def test_visitor_identity_is_stable_per_browser_and_isolated_between_browsers(
    service_session_factory: sessionmaker[Session],
) -> None:
    first_browser = _test_client(service_session_factory)
    second_browser = _test_client(service_session_factory)

    first = first_browser.post("/api/auth/visitor")
    first_again = first_browser.post("/api/auth/visitor")
    second = second_browser.post("/api/auth/visitor")

    assert first.status_code == 200
    assert first_again.status_code == 200
    assert second.status_code == 200
    assert first.json()["buyer_id"] == first_again.json()["buyer_id"]
    assert first.json()["buyer_id"] != second.json()["buyer_id"]
    assert first.json()["buyer_id"].startswith("buyer-")
    assert "HttpOnly" in first.headers["set-cookie"]


def test_authentication_endpoints_fail_closed_without_secret(
    service_session_factory: sessionmaker[Session],
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="mysql+pymysql://test:test@127.0.0.1/test",
    )
    client = _test_client(service_session_factory, settings=settings)

    response = client.post("/api/auth/visitor")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "authentication service is not configured"
    }
