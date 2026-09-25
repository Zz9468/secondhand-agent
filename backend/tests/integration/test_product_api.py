from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import (
    SELLER_SESSION_COOKIE,
    create_identity_token,
    hash_password,
)
from app.db.models import SellerAccount
from app.main import create_app
from app.services.negotiation_service import NegotiationService

pytestmark = pytest.mark.mysql_integration

TEST_SETTINGS = Settings(
    _env_file=None,
    database_url="mysql+pymysql://test:test@127.0.0.1/test",
    auth_secret="a-secure-test-secret-with-32-characters",
)


def _test_client(session_factory: sessionmaker[Session]) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_session_factory_dependency] = (
        lambda: session_factory
    )
    application.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    return TestClient(application)


def _create_seller(
    session_factory: sessionmaker[Session],
    *,
    username: str,
) -> str:
    seller_id = f"seller-{uuid4().hex}"
    with session_factory() as db, db.begin():
        db.add(
            SellerAccount(
                id=seller_id,
                username=username,
                password_hash=hash_password("product-api-test-password"),
                is_active=True,
            )
        )
    return seller_id


def _authenticate_seller(client: TestClient, seller_id: str) -> None:
    assert TEST_SETTINGS.auth_secret is not None
    token = create_identity_token(
        subject=seller_id,
        kind="seller",
        secret=TEST_SETTINGS.auth_secret.get_secret_value(),
        lifetime=timedelta(minutes=30),
    )
    client.cookies.set(SELLER_SESSION_COOKIE, token)


def _product_payload(*, status: str = "DRAFT") -> dict[str, object]:
    return {
        "title": "卖家新建商品",
        "description": "用于验证商品管理与隐私边界。",
        "listed_price": "3000.00",
        "status": status,
        "policy": {
            "minimum_net_price": "2700.00",
            "auto_accept_threshold": "2850.00",
            "negotiation_style": "BALANCED",
            "max_rounds": 6,
        },
    }


def test_public_products_only_expose_available_public_fields(
    service_session_factory: sessionmaker[Session],
) -> None:
    seller_id = _create_seller(
        service_session_factory,
        username=f"catalog-{uuid4().hex}",
    )
    seller_client = _test_client(service_session_factory)
    _authenticate_seller(seller_client, seller_id)

    available = seller_client.post(
        "/api/products",
        json=_product_payload(status="AVAILABLE"),
    )
    draft = seller_client.post("/api/products", json=_product_payload())
    anonymous = _test_client(service_session_factory)
    product_id = available.json()["id"]

    public_list = anonymous.get("/api/products")
    public_detail = anonymous.get(f"/api/products/{product_id}")
    hidden_detail = anonymous.get(f"/api/products/{draft.json()['id']}")
    unauthenticated_create = anonymous.post(
        "/api/products",
        json=_product_payload(),
    )

    assert available.status_code == 201
    assert draft.status_code == 201
    assert public_list.status_code == 200
    listed_ids = {item["id"] for item in public_list.json()["products"]}
    assert product_id in listed_ids
    assert draft.json()["id"] not in listed_ids
    assert public_detail.status_code == 200
    assert set(public_detail.json()) == {
        "id",
        "title",
        "description",
        "listed_price",
        "status",
    }
    assert "2700" not in public_detail.text
    assert hidden_detail.status_code == 404
    assert unauthenticated_create.status_code == 401


def test_seller_manages_only_owned_products_and_policy_versions(
    service_session_factory: sessionmaker[Session],
) -> None:
    owner_id = _create_seller(
        service_session_factory,
        username=f"owner-{uuid4().hex}",
    )
    other_id = _create_seller(
        service_session_factory,
        username=f"other-{uuid4().hex}",
    )
    owner = _test_client(service_session_factory)
    other = _test_client(service_session_factory)
    _authenticate_seller(owner, owner_id)
    _authenticate_seller(other, other_id)

    created = owner.post(
        "/api/products",
        json=_product_payload(status="AVAILABLE"),
    )
    product_id = created.json()["id"]
    listed = owner.get("/api/seller/products")
    updated = owner.put(
        f"/api/seller/products/{product_id}",
        json={
            "title": "修改后的商品",
            "description": "商品公开信息已更新。",
            "listed_price": "3100.00",
            "status": "AVAILABLE",
        },
    )
    policy_payload = {
        "minimum_net_price": "2750.00",
        "auto_accept_threshold": "2900.00",
        "negotiation_style": "FIRM",
        "max_rounds": 4,
        "expected_version": 1,
    }
    policy_updated = owner.put(
        f"/api/seller/products/{product_id}/policy",
        json=policy_payload,
    )
    stale_update = owner.put(
        f"/api/seller/products/{product_id}/policy",
        json=policy_payload,
    )
    forbidden_read = other.get(f"/api/seller/products/{product_id}")
    forbidden_update = other.put(
        f"/api/seller/products/{product_id}",
        json={
            "title": "越权修改",
            "description": "不应成功",
            "listed_price": "1.00",
            "status": "UNAVAILABLE",
        },
    )

    assert created.status_code == 201
    assert created.json()["policy"]["version"] == 1
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["products"]] == [product_id]
    assert updated.status_code == 200
    assert updated.json()["title"] == "修改后的商品"
    assert policy_updated.status_code == 200
    assert policy_updated.json()["policy"] == {
        "minimum_net_price": "2750.00",
        "auto_accept_threshold": "2900.00",
        "negotiation_style": "FIRM",
        "max_rounds": 4,
        "version": 2,
    }
    assert stale_update.status_code == 409
    assert forbidden_read.status_code == 404
    assert forbidden_update.status_code == 404

    buyer = _test_client(service_session_factory)
    visitor = buyer.post("/api/auth/visitor")
    assert visitor.status_code == 200
    negotiation = buyer.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    assert negotiation.status_code == 200
    state = NegotiationService(service_session_factory).get_state(
        session_id=negotiation.json()["session_id"],
        buyer_id=visitor.json()["buyer_id"],
    )
    assert state.negotiation_style.value == "FIRM"
    assert state.max_rounds == 4
