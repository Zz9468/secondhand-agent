from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ModelExecutionTask,
    ModelTaskErrorCategory,
    ModelTaskStatus,
    ModelTaskType,
    NegotiationSession,
    Product,
    SellerPolicy,
    UserAccount,
)
from app.services.approval_service import ApprovalService
from app.services.errors import (
    InvalidModelExecutionTaskError,
    ModelExecutionTaskConflictError,
    ModelExecutionTaskLeaseError,
)
from app.services.model_task_service import ModelTaskService, ModelUsage
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


def test_create_task_captures_versions_and_replays_identical_input(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    service = ModelTaskService(service_session_factory)
    original_input = {
        "buyer_message": {"content": "请介绍一下商品"},
        "history": [],
    }

    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:request-001",
        session_id=session_id,
        input_snapshot=original_input,
    )
    original_input["buyer_message"]["content"] = "篡改后的内容"  # type: ignore[index]
    replayed = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:request-001",
        session_id=session_id,
        input_snapshot={
            "history": [],
            "buyer_message": {"content": "请介绍一下商品"},
        },
    )

    assert created.idempotent_replay is False
    assert replayed.idempotent_replay is True
    assert replayed.task.id == created.task.id
    assert replayed.task.session_version == 1
    assert replayed.task.policy_version == 1
    assert replayed.task.status is ModelTaskStatus.PENDING
    assert replayed.task.input_snapshot == {
        "buyer_message": {"content": "请介绍一下商品"},
        "history": [],
    }
    assert len(replayed.task.input_snapshot_hash) == 64
    with service_session_factory() as db:
        count = db.scalar(
            select(func.count()).select_from(ModelExecutionTask).where(
                ModelExecutionTask.task_type == ModelTaskType.CHAT_DECISION,
                ModelExecutionTask.business_key
                == f"chat:{session_id}:request-001",
            )
        )
        assert count == 1


def test_same_business_key_rejects_different_input(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    service = ModelTaskService(service_session_factory)
    business_key = f"chat:{session_id}:request-conflict"
    service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=business_key,
        session_id=session_id,
        input_snapshot={"buyer_message": "原始消息"},
    )

    with pytest.raises(ModelExecutionTaskConflictError):
        service.create_task(
            task_type=ModelTaskType.CHAT_DECISION,
            business_key=business_key,
            session_id=session_id,
            input_snapshot={"buyer_message": "不同消息"},
        )


def test_approval_followup_task_captures_reviewed_approval_snapshot(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    offer = NegotiationService(service_session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
    )
    approval_service = ApprovalService(service_session_factory)
    approval = approval_service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="报价位于卖家审批区",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
    approval_service.approve_request(
        seller_id=seller_id,
        approval_id=approval.id,
        request_id=f"approval-followup:{approval.id}",
    )

    created = ModelTaskService(service_session_factory).create_task(
        task_type=ModelTaskType.APPROVAL_FOLLOWUP,
        business_key=f"approval-followup:{approval.id}",
        session_id=session_id,
        offer_id=offer.id,
        approval_id=approval.id,
        input_snapshot={
            "approval_id": approval.id,
            "event": "APPROVED",
            "offer_id": offer.id,
        },
    )

    assert created.task.offer_id == offer.id
    assert created.task.approval_id == approval.id
    assert created.task.policy_version == approval.policy_version
    assert created.task.session_version > 1


def test_approval_followup_task_requires_reviewed_consistent_references(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)

    with pytest.raises(InvalidModelExecutionTaskError):
        ModelTaskService(service_session_factory).create_task(
            task_type=ModelTaskType.APPROVAL_FOLLOWUP,
            business_key=f"approval-followup:{session_id}:missing",
            session_id=session_id,
            input_snapshot={"event": "APPROVED"},
        )


def test_lease_and_complete_task_persists_result_and_usage(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    service = ModelTaskService(service_session_factory)
    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:complete",
        session_id=session_id,
        input_snapshot={"buyer_message": "最低多少钱？"},
    )
    leased_at = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)

    leased = service.lease_next(
        worker_id="worker-01",
        task_types=(ModelTaskType.CHAT_DECISION,),
        lease_seconds=30,
        now=leased_at,
    )
    assert leased is not None
    assert leased.id == created.task.id
    assert leased.status is ModelTaskStatus.RUNNING
    assert leased.attempt_count == 1
    assert leased.lease_token is not None

    with pytest.raises(ModelExecutionTaskLeaseError):
        service.complete_success(
            task_id=leased.id,
            lease_token="wrong-token",
            result_snapshot={"action": "INQUIRY"},
            usage=ModelUsage(provider="qwen", model_name="qwen-plus"),
            now=leased_at + timedelta(seconds=1),
        )

    completed = service.complete_success(
        task_id=leased.id,
        lease_token=leased.lease_token,
        result_snapshot={"action": "INQUIRY", "dialogue_acts": []},
        usage=ModelUsage(
            provider="qwen",
            model_name="qwen-plus",
            input_tokens=120,
            output_tokens=35,
            cached_input_tokens=20,
            total_tokens=155,
            input_price_per_million=Decimal("2.00000000"),
            output_price_per_million=Decimal("8.00000000"),
            cached_input_price_per_million=Decimal("0.50000000"),
            estimated_cost=Decimal("0.00012345"),
            cost_currency="CNY",
        ),
        now=leased_at + timedelta(seconds=2),
    )

    assert completed.status is ModelTaskStatus.SUCCEEDED
    assert completed.lease_token is None
    assert completed.result_snapshot == {
        "action": "INQUIRY",
        "dialogue_acts": [],
    }
    assert completed.total_tokens == 155
    assert completed.cached_input_tokens == 20
    assert completed.input_price_per_million == Decimal("2.00000000")
    assert completed.estimated_cost == Decimal("0.00012345")
    assert completed.cost_currency == "CNY"
    assert service.lease_next(worker_id="worker-02", now=leased_at) is None


def test_retry_schedule_and_expired_lease_are_recoverable(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    service = ModelTaskService(service_session_factory)
    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:retry",
        session_id=session_id,
        input_snapshot={"buyer_message": "可以包邮吗？"},
    )
    started_at = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)
    first = service.lease_next(
        worker_id="worker-a",
        lease_seconds=10,
        now=started_at,
    )
    assert first is not None and first.lease_token is not None

    waiting = service.defer_retry(
        task_id=first.id,
        lease_token=first.lease_token,
        error_category=ModelTaskErrorCategory.MODEL_TIMEOUT,
        error_message=" provider timeout \n request id removed ",
        next_retry_at=started_at + timedelta(seconds=30),
        now=started_at + timedelta(seconds=1),
    )
    assert waiting.status is ModelTaskStatus.RETRY_WAIT
    assert waiting.last_error_message == "provider timeout request id removed"
    assert (
        service.lease_next(
            worker_id="worker-b",
            now=started_at + timedelta(seconds=20),
        )
        is None
    )

    second = service.lease_next(
        worker_id="worker-b",
        lease_seconds=10,
        now=started_at + timedelta(seconds=31),
    )
    assert second is not None and second.lease_token is not None
    assert second.attempt_count == 2
    assert (
        service.lease_next(
            worker_id="worker-c",
            now=started_at + timedelta(seconds=35),
        )
        is None
    )

    reclaimed = service.lease_next(
        worker_id="worker-c",
        lease_seconds=10,
        now=started_at + timedelta(seconds=42),
    )
    assert reclaimed is not None and reclaimed.lease_token is not None
    assert reclaimed.id == created.task.id
    assert reclaimed.attempt_count == 3
    assert reclaimed.lease_token != second.lease_token

    stale = service.mark_stale(
        task_id=reclaimed.id,
        lease_token=reclaimed.lease_token,
        error_message="会话版本已经变化",
        usage=ModelUsage(
            provider="qwen",
            model_name="qwen-plus",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        ),
        now=started_at + timedelta(seconds=43),
    )
    assert stale.status is ModelTaskStatus.STALE
    assert stale.last_error_category is ModelTaskErrorCategory.BUSINESS_CONFLICT
    assert stale.completed_at is not None
    assert stale.total_tokens == 15
    assert stale.lease_owner is None


def test_concurrent_workers_lease_only_one_task(mysql_engine: Engine) -> None:
    committed_factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session_id, _ = create_negotiation(committed_factory)
    service = ModelTaskService(committed_factory)
    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:concurrent-lease",
        session_id=session_id,
        input_snapshot={"buyer_message": "并发领取测试"},
    )
    barrier = Barrier(2)
    leased_at = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    def lease(worker_id: str) -> object:
        barrier.wait()
        return ModelTaskService(committed_factory).lease_next(
            worker_id=worker_id,
            lease_seconds=30,
            now=leased_at,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(lease, ("concurrent-worker-a", "concurrent-worker-b"))
            )

        leased = [item for item in results if item is not None]
        assert len(leased) == 1
        assert leased[0].id == created.task.id
        assert service.get_task(task_id=created.task.id).attempt_count == 1
    finally:
        _delete_committed_negotiation(committed_factory, session_id=session_id)


def test_concurrent_identical_creation_returns_one_task(mysql_engine: Engine) -> None:
    committed_factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session_id, _ = create_negotiation(committed_factory)
    barrier = Barrier(2)

    def create() -> object:
        barrier.wait()
        return ModelTaskService(committed_factory).create_task(
            task_type=ModelTaskType.CHAT_DECISION,
            business_key=f"chat:{session_id}:concurrent-create",
            session_id=session_id,
            input_snapshot={"buyer_message": "重复请求只创建一个任务"},
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: create(), range(2)))

        assert len({item.task.id for item in results}) == 1
        assert sorted(item.idempotent_replay for item in results) == [False, True]
        with committed_factory() as db:
            count = db.scalar(
                select(func.count()).select_from(ModelExecutionTask).where(
                    ModelExecutionTask.business_key
                    == f"chat:{session_id}:concurrent-create"
                )
            )
            assert count == 1
    finally:
        _delete_committed_negotiation(committed_factory, session_id=session_id)


def test_terminal_failure_clears_lease_and_cannot_be_completed_again(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    service = ModelTaskService(service_session_factory)
    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:terminal-failure",
        session_id=session_id,
        input_snapshot={"buyer_message": "产生无效结构化输出"},
    )
    started_at = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
    leased = service.lease_next(worker_id="worker-failure", now=started_at)
    assert leased is not None and leased.lease_token is not None

    failed = service.fail_task(
        task_id=created.task.id,
        lease_token=leased.lease_token,
        error_category=ModelTaskErrorCategory.INVALID_OUTPUT,
        error_message="结构化输出不符合契约",
        now=started_at + timedelta(seconds=1),
    )

    assert failed.status is ModelTaskStatus.FAILED
    assert failed.completed_at is not None
    assert failed.lease_token is None
    with pytest.raises(ModelExecutionTaskLeaseError):
        service.mark_stale(
            task_id=failed.id,
            lease_token=leased.lease_token,
            now=started_at + timedelta(seconds=2),
        )


def test_lease_refuses_attempt_beyond_persisted_limit(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    service = ModelTaskService(service_session_factory)
    created = service.create_task(
        task_type=ModelTaskType.CHAT_DECISION,
        business_key=f"chat:{session_id}:bounded-attempts",
        session_id=session_id,
        input_snapshot={"buyer_message": "超时重试上限测试"},
        max_attempts=1,
    )
    started_at = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    first = service.lease_task(
        task_id=created.task.id,
        worker_id="bounded-worker-a",
        now=started_at,
    )
    assert first is not None and first.lease_token is not None
    service.defer_retry(
        task_id=first.id,
        lease_token=first.lease_token,
        error_category=ModelTaskErrorCategory.MODEL_TIMEOUT,
        error_message="模型调用超时",
        next_retry_at=started_at + timedelta(seconds=2),
        now=started_at + timedelta(seconds=1),
    )

    exhausted = service.lease_task(
        task_id=created.task.id,
        worker_id="bounded-worker-b",
        now=started_at + timedelta(seconds=3),
    )

    assert exhausted is not None
    assert exhausted.status is ModelTaskStatus.FAILED
    assert exhausted.attempt_count == 1
    assert exhausted.lease_token is None
    assert exhausted.completed_at is not None
    assert service.lease_next(
        worker_id="bounded-worker-c",
        now=started_at + timedelta(seconds=4),
    ) is None


def _delete_committed_negotiation(
    session_factory: sessionmaker[Session],
    *,
    session_id: int,
) -> None:
    with session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, session_id)
        if negotiation is None:
            return
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
        buyer_id = negotiation.buyer_id
        db.delete(negotiation)
        db.flush()
        db.execute(delete(SellerPolicy).where(SellerPolicy.product_id == product.id))
        db.delete(product)
        db.flush()
        for account_id in (seller_id, buyer_id):
            account = db.get(UserAccount, account_id)
            if account is not None:
                db.delete(account)
