from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.models import NegotiationSession, UserAccount
from app.main import create_app
from app.services.negotiation_service import NegotiationService
from tests.integration.auth_helpers import authenticate_user
from tests.integration.factories import create_user_account

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
            UserAccount(
                id=seller_id,
                username=username,
                display_name="商品接口测试卖家",
                password_hash=hash_password("product-api-test-password"),
                is_active=True,
            )
        )
    return seller_id


def _authenticate_seller(client: TestClient, seller_id: str) -> None:
    authenticate_user(client, user_id=seller_id, settings=TEST_SETTINGS)


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
    username = f"catalog-{uuid4().hex}"
    seller_id = _create_seller(
        service_session_factory,
        username=username,
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
        "seller",
    }
    assert public_detail.json()["seller"] == {
        "id": seller_id,
        "display_name": "商品接口测试卖家",
    }
    assert username not in public_detail.text
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
    buyer_id = create_user_account(
        service_session_factory,
        display_name="商品接口测试买家",
    )
    authenticate_user(buyer, user_id=buyer_id, settings=TEST_SETTINGS)
    negotiation = buyer.post(
        "/api/negotiations",
        json={"product_id": product_id},
    )
    assert negotiation.status_code == 200
    state = NegotiationService(service_session_factory).get_state(
        session_id=negotiation.json()["session_id"],
        buyer_id=buyer_id,
    )
    assert state.negotiation_style.value == "FIRM"
    assert state.max_rounds == 4


def test_registered_accounts_share_one_session_across_buyer_and_seller_modes(
    service_session_factory: sessionmaker[Session],
) -> None:
    """统一账号无需角色切换即可同时购买他人商品并管理自己的商品。"""

    suffix = uuid4().hex
    account = _test_client(service_session_factory)
    other = _test_client(service_session_factory)
    registered = account.post(
        "/api/auth/register",
        json={
            "username": f"workspace-{suffix}",
            "display_name": "工作台测试用户",
            "password": "Strong-workspace-password-2026",
        },
    )
    other_registered = other.post(
        "/api/auth/register",
        json={
            "username": f"workspace-other-{suffix}",
            "display_name": "工作台测试买家",
            "password": "Strong-workspace-password-2026",
        },
    )

    empty_products = account.get("/api/seller/products")
    empty_approvals = account.get("/api/seller/approvals")
    empty_negotiations = account.get("/api/seller/negotiations")
    own_product = account.post(
        "/api/products",
        json=_product_payload(status="AVAILABLE"),
    )
    other_product = other.post(
        "/api/products",
        json=_product_payload(status="AVAILABLE"),
    )

    self_negotiation = account.post(
        "/api/negotiations",
        json={"product_id": own_product.json()["id"]},
    )
    buying_session = account.post(
        "/api/negotiations",
        json={"product_id": other_product.json()["id"]},
    )
    incoming_session = other.post(
        "/api/negotiations",
        json={"product_id": own_product.json()["id"]},
    )
    owned_products = account.get("/api/seller/products")
    buyer_history = account.get("/api/buyer/negotiations")
    seller_history = account.get("/api/seller/negotiations")

    assert registered.status_code == 201
    assert other_registered.status_code == 201
    assert empty_products.json() == {"products": []}
    assert empty_approvals.json() == {"approvals": []}
    assert empty_negotiations.json() == {"negotiations": []}
    assert own_product.status_code == 201
    assert other_product.status_code == 201
    assert self_negotiation.status_code == 409
    assert buying_session.status_code == 200
    assert incoming_session.status_code == 200
    assert [item["id"] for item in owned_products.json()["products"]] == [
        own_product.json()["id"]
    ]
    assert [item["id"] for item in buyer_history.json()["negotiations"]] == [
        buying_session.json()["session_id"]
    ]
    assert [item["id"] for item in seller_history.json()["negotiations"]] == [
        incoming_session.json()["session_id"]
    ]
    assert (
        seller_history.json()["negotiations"][0]["buyer_display_name"]
        == "工作台测试买家"
    )


def test_public_catalog_and_seller_pages_are_read_only_and_hide_private_fields(
    service_session_factory: sessionmaker[Session],
) -> None:
    username = f"public-seller-{uuid4().hex}"
    seller_id = _create_seller(service_session_factory, username=username)
    seller = _test_client(service_session_factory)
    _authenticate_seller(seller, seller_id)
    first = seller.post(
        "/api/products",
        json=_product_payload(status="AVAILABLE"),
    )
    second_payload = _product_payload(status="AVAILABLE")
    second_payload["title"] = "另一件公开商品"
    second = seller.post("/api/products", json=second_payload)
    draft = seller.post("/api/products", json=_product_payload())
    hidden_seller_id = _create_seller(
        service_session_factory,
        username=f"draft-only-{uuid4().hex}",
    )
    hidden_seller = _test_client(service_session_factory)
    _authenticate_seller(hidden_seller, hidden_seller_id)
    hidden_product = hidden_seller.post("/api/products", json=_product_payload())
    anonymous = _test_client(service_session_factory)

    with service_session_factory() as db:
        session_count_before = db.scalar(select(func.count(NegotiationSession.id)))

    seller_list = anonymous.get("/api/sellers")
    seller_detail = anonymous.get(f"/api/sellers/{seller_id}")
    seller_products = anonymous.get(f"/api/sellers/{seller_id}/products")
    filtered_products = anonymous.get(
        "/api/products",
        params={"seller_id": seller_id},
    )
    refreshed_detail = anonymous.get(f"/api/products/{first.json()['id']}")
    missing_seller = anonymous.get("/api/sellers/not-a-real-seller")
    hidden_seller_detail = anonymous.get(f"/api/sellers/{hidden_seller_id}")

    with service_session_factory() as db:
        session_count_after = db.scalar(select(func.count(NegotiationSession.id)))

    assert seller_list.status_code == 200
    assert hidden_product.status_code == 201
    listed_seller = next(
        item for item in seller_list.json()["sellers"] if item["id"] == seller_id
    )
    assert listed_seller == {
        "id": seller_id,
        "display_name": "商品接口测试卖家",
        "available_product_count": 2,
    }
    assert hidden_seller_id not in {
        item["id"] for item in seller_list.json()["sellers"]
    }
    assert seller_detail.status_code == 200
    assert seller_detail.json() == listed_seller
    assert seller_products.status_code == 200
    assert filtered_products.status_code == 200
    expected_product_ids = {first.json()["id"], second.json()["id"]}
    seller_product_ids = {
        item["id"] for item in seller_products.json()["products"]
    }
    filtered_product_ids = {
        item["id"] for item in filtered_products.json()["products"]
    }
    assert seller_product_ids == expected_product_ids
    assert filtered_product_ids == expected_product_ids
    assert draft.json()["id"] not in seller_product_ids
    assert refreshed_detail.status_code == 200
    assert missing_seller.status_code == 404
    assert hidden_seller_detail.status_code == 404
    public_responses = "".join(
        response.text
        for response in (
            seller_list,
            seller_detail,
            seller_products,
            filtered_products,
            refreshed_detail,
        )
    )
    assert username not in public_responses
    assert "product-api-test-password" not in public_responses
    assert "password_hash" not in public_responses
    assert "minimum_net_price" not in public_responses
    assert "auto_accept_threshold" not in public_responses
    assert session_count_after == session_count_before
