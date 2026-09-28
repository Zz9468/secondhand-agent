import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory_dependency
from app.core.config import Settings, get_settings
from app.db.models import (
    Message,
    ModelTaskErrorCategory,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    Product,
)
from app.main import create_app
from app.services.model_task_service import ModelTaskService
from tests.integration.auth_helpers import authenticate_user
from tests.integration.factories import create_negotiation, create_user_account

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


def _failed_chat_task(
    session_factory: sessionmaker[Session],
) -> tuple[str, int, int, str]:
    session_id, _ = create_negotiation(session_factory)
    reply_request_id = "manual-recovery-request-001:agent"
    with session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id

    service = ModelTaskService(session_factory)
    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:manual-recovery",
        session_id=session_id,
        input_snapshot={"reply_request_id": reply_request_id},
        max_attempts=1,
    )
    leased = service.lease_task(
        task_id=created.task.id,
        worker_id="failed-api-worker",
    )
    assert leased is not None and leased.lease_token is not None
    service.fail_task(
        task_id=leased.id,
        lease_token=leased.lease_token,
        error_category=ModelTaskErrorCategory.MODEL_TIMEOUT,
        error_message="模型调用超时",
    )
    return seller_id, session_id, leased.id, reply_request_id


def test_seller_can_list_retry_and_terminate_owned_failed_task(
    service_session_factory: sessionmaker[Session],
) -> None:
    seller_id, session_id, task_id, reply_request_id = _failed_chat_task(
        service_session_factory
    )
    client = _test_client(service_session_factory)
    authenticate_user(client, user_id=seller_id, settings=TEST_SETTINGS)

    listed = client.get("/api/seller/model-tasks")
    retried = client.post(
        f"/api/seller/model-tasks/{task_id}/retry",
        json={"reason": "模型服务已恢复"},
    )
    terminated = client.post(
        f"/api/seller/model-tasks/{task_id}/terminate",
        json={"reason": "改为由卖家手工回复"},
    )

    assert listed.status_code == 200
    task_payload = next(
        item for item in listed.json()["tasks"] if item["id"] == task_id
    )
    assert task_payload["status"] == "FAILED"
    assert task_payload["last_error_category"] == "MODEL_TIMEOUT"
    assert "input_snapshot" not in task_payload
    assert "business_key" not in task_payload
    assert "lease_token" not in task_payload
    assert retried.status_code == 200
    assert retried.json()["status"] == "PENDING"
    assert retried.json()["max_attempts"] == 2
    assert retried.json()["manual_retry_count"] == 1
    assert retried.json()["last_manual_action"] == "RETRY"
    assert terminated.status_code == 200
    assert terminated.json()["status"] == "CANCELLED"
    assert terminated.json()["last_manual_action"] == "TERMINATE"
    with service_session_factory() as db:
        message = db.scalar(
            select(Message).where(
                Message.session_id == session_id,
                Message.request_id == reply_request_id,
            )
        )
        assert message is not None
        assert message.agent_outcome == "SAFE_FAILURE"


def test_other_seller_cannot_see_or_recover_task(
    service_session_factory: sessionmaker[Session],
) -> None:
    _, _, task_id, _ = _failed_chat_task(service_session_factory)
    outsider_id = create_user_account(
        service_session_factory,
        display_name="外部卖家",
    )
    outsider = _test_client(service_session_factory)
    authenticate_user(outsider, user_id=outsider_id, settings=TEST_SETTINGS)

    listed = outsider.get("/api/seller/model-tasks")
    retried = outsider.post(
        f"/api/seller/model-tasks/{task_id}/retry",
        json={"reason": None},
    )

    assert listed.status_code == 200
    assert listed.json()["tasks"] == []
    assert retried.status_code == 404
    task = ModelTaskService(service_session_factory).get_task(task_id=task_id)
    assert task.status is ModelTaskStatus.FAILED
