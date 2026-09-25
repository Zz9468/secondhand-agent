from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier, Lock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.agent.approval_followup import (
    ApprovalFollowupDraft,
    ApprovalFollowupEvent,
    ApprovalFollowupRequest,
)
from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    Message,
    MessageRole,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    OfferStatus,
    Product,
    SellerAccount,
    SellerPolicy,
)
from app.services.approval_service import ApprovalService
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, ShippingPayer
from app.workers.approval_processor import ApprovalProcessor
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


class RecordingFollowupProvider:
    def __init__(self, *, failures: int = 0) -> None:
        self._remaining_failures = failures
        self._lock = Lock()
        self.requests: list[ApprovalFollowupRequest] = []

    def draft(self, request: ApprovalFollowupRequest) -> ApprovalFollowupDraft:
        with self._lock:
            self.requests.append(request)
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                raise RuntimeError("模拟模型超时")
        return ApprovalFollowupDraft(
            acknowledged_event=request.event,
            reason="按可信审批结果通知买家",
            reply="模型候选稿不会直接发送。",
        )


def _create_reviewed_approval(
    session_factory: sessionmaker[Session],
    *,
    approved: bool,
    request_id: str,
) -> tuple[int, int, int]:
    session_id, buyer_id = create_negotiation(session_factory)
    offer = NegotiationService(session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=OfferTerms(
            buyer_payment=Decimal("2800.00"),
            shipping_paid_by=ShippingPayer.BUYER,
        ),
        additional_terms={"delivery_method": "shipping"},
    )
    approval_service = ApprovalService(session_factory)
    approval = approval_service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="报价位于卖家审批区",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
    if approved:
        approval_service.approve_request(
            seller_id=seller_id,
            approval_id=approval.id,
            request_id=request_id,
        )
    else:
        approval_service.reject_request(
            seller_id=seller_id,
            approval_id=approval.id,
            request_id=request_id,
        )
    return session_id, offer.id, approval.id


def test_worker_sends_approved_followup_once_and_restores_active_session(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, offer_id, approval_id = _create_reviewed_approval(
        service_session_factory,
        approved=True,
        request_id="worker-approved-001",
    )
    provider = RecordingFollowupProvider()
    processor = ApprovalProcessor(service_session_factory, provider)

    first = processor.process_next()
    replay = processor.process_next()

    assert first is not None
    assert first.status is ApprovalFollowupStatus.SENT
    assert first.message_id is not None
    assert replay is None
    assert len(provider.requests) == 1
    with service_session_factory() as db:
        approval = db.get(ApprovalRequest, approval_id)
        negotiation = db.get(NegotiationSession, session_id)
        offer = db.get(Offer, offer_id)
        messages = tuple(
            db.scalars(
                select(Message).where(Message.session_id == session_id)
            )
        )
        assert approval is not None
        assert approval.followup_status is ApprovalFollowupStatus.SENT
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE
        assert offer is not None
        assert offer.status is OfferStatus.ACCEPTED
        assert len(messages) == 1
        assert messages[0].role is MessageRole.AGENT
        assert messages[0].request_id == "worker-approved-001"
        assert messages[0].formal_offer_id == offer_id
        assert "不代表已经成交" in messages[0].content


def test_worker_sends_rejection_and_retries_failed_model_call(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, offer_id, approval_id = _create_reviewed_approval(
        service_session_factory,
        approved=False,
        request_id="worker-rejected-001",
    )
    provider = RecordingFollowupProvider(failures=1)
    processor = ApprovalProcessor(service_session_factory, provider)

    failed = processor.process_next()
    retried = processor.process_next()

    assert failed is not None
    assert failed.status is ApprovalFollowupStatus.FAILED
    assert retried is not None
    assert retried.status is ApprovalFollowupStatus.SENT
    assert len(provider.requests) == 2
    with service_session_factory() as db:
        approval = db.get(ApprovalRequest, approval_id)
        negotiation = db.get(NegotiationSession, session_id)
        offer = db.get(Offer, offer_id)
        messages = tuple(
            db.scalars(
                select(Message).where(Message.session_id == session_id)
            )
        )
        assert approval is not None
        assert approval.followup_status is ApprovalFollowupStatus.SENT
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE
        assert offer is not None
        assert offer.status is OfferStatus.REJECTED
        assert len(messages) == 1
        assert messages[0].formal_offer_id is None
        assert "未同意" in messages[0].content


def test_worker_invalidates_approval_when_policy_changes_after_review(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, offer_id, approval_id = _create_reviewed_approval(
        service_session_factory,
        approved=True,
        request_id="worker-invalidated-001",
    )
    with service_session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        policy = db.scalar(
            select(SellerPolicy).where(
                SellerPolicy.product_id == negotiation.product_id
            )
        )
        assert policy is not None
        policy.version += 1

    provider = RecordingFollowupProvider()
    result = ApprovalProcessor(service_session_factory, provider).process_next()

    assert result is not None
    assert result.status is ApprovalFollowupStatus.SENT
    assert provider.requests[0].event is ApprovalFollowupEvent.INVALIDATED
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        offer = db.get(Offer, offer_id)
        message = db.scalar(
            select(Message).where(Message.session_id == session_id)
        )
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE
        assert offer is not None
        assert offer.status is OfferStatus.WITHDRAWN
        assert message is not None
        assert "不再有效" in message.content


def test_concurrent_workers_send_only_one_followup(mysql_engine: Engine) -> None:
    committed_factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session_id, _, approval_id = _create_reviewed_approval(
        committed_factory,
        approved=True,
        request_id="worker-concurrent-001",
    )
    provider = RecordingFollowupProvider()
    barrier = Barrier(2)

    def process() -> object:
        barrier.wait()
        return ApprovalProcessor(committed_factory, provider).process_next()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: process(), range(2)))

        assert sum(result is not None for result in results) == 1
        assert len(provider.requests) == 1
        with committed_factory() as db:
            approval = db.get(ApprovalRequest, approval_id)
            message_count = len(
                tuple(
                    db.scalars(
                        select(Message).where(Message.session_id == session_id)
                    )
                )
            )
            assert approval is not None
            assert approval.followup_status is ApprovalFollowupStatus.SENT
            assert message_count == 1
    finally:
        _delete_committed_negotiation(committed_factory, session_id=session_id)


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
        negotiation.current_offer_id = None
        db.flush()
        db.execute(
            delete(ApprovalRequest).where(ApprovalRequest.session_id == session_id)
        )
        db.execute(delete(Message).where(Message.session_id == session_id))
        db.execute(delete(Offer).where(Offer.session_id == session_id))
        db.delete(negotiation)
        db.flush()
        db.execute(
            delete(SellerPolicy).where(SellerPolicy.product_id == product.id)
        )
        db.delete(product)
        db.flush()
        seller = db.get(SellerAccount, seller_id)
        if seller is not None:
            db.delete(seller)
