from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    NegotiationSession,
    NegotiationStatus,
    Offer,
    OfferStatus,
    Product,
    SellerAccount,
    SellerPolicy,
)
from app.schemas.approval import ApprovalResponse
from app.services.approval_service import ApprovalService
from app.services.errors import (
    ApprovalConflictError,
    ApprovalNotAuthorizedError,
    ApprovalNotExpiredError,
    ApprovalNotFoundError,
    NegotiationNotFoundError,
)
from app.services.negotiation_service import NegotiationService, OfferSnapshot
from app.services.pricing_service import OfferTerms, ShippingPayer
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


def buyer_terms(price: str) -> OfferTerms:
    return OfferTerms(
        buyer_payment=Decimal(price),
        shipping_paid_by=ShippingPayer.BUYER,
    )


def create_buyer_offer(
    session_factory: sessionmaker[Session],
    *,
    price: str = "2800.00",
    offer_expires_at: datetime | None = None,
    additional_terms: dict[str, object] | None = None,
) -> tuple[int, str, OfferSnapshot]:
    session_id, buyer_id = create_negotiation(session_factory)
    offer = NegotiationService(session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=buyer_terms(price),
        expires_at=offer_expires_at,
        additional_terms=additional_terms,
    )
    return session_id, buyer_id, offer


def test_create_query_and_repeat_approval_request(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, offer = create_buyer_offer(service_session_factory)
    service = ApprovalService(service_session_factory)
    expires_at = datetime.now() + timedelta(hours=1)

    created = service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="买家报价位于审批区，请卖家确认。",
        expires_at=expires_at,
    )
    repeated = service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="重复请求不得产生新记录。",
        expires_at=expires_at,
    )
    queried = service.get_request(
        session_id=session_id,
        buyer_id=buyer_id,
        approval_id=created.id,
    )
    history = service.list_for_session(
        session_id=session_id,
        buyer_id=buyer_id,
    )

    assert created.status is ApprovalStatus.PENDING
    assert repeated.id == created.id
    assert queried == created
    assert history == (created,)
    assert ApprovalResponse.model_validate(created).status is ApprovalStatus.PENDING
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        stored = db.get(ApprovalRequest, created.id)
        assert negotiation is not None
        assert stored is not None
        assert negotiation.status is NegotiationStatus.WAITING_APPROVAL
        assert stored.pending_session_id == session_id
        count = db.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.session_id == session_id)
        )
        assert count == 1

    with pytest.raises(NegotiationNotFoundError):
        service.get_request(
            session_id=session_id,
            buyer_id="another-buyer",
            approval_id=created.id,
        )


def test_cancel_and_expire_restore_active_session(
    service_session_factory: sessionmaker[Session],
) -> None:
    first_session_id, first_buyer_id, first_offer = create_buyer_offer(
        service_session_factory
    )
    service = ApprovalService(service_session_factory)
    first = service.create_request(
        session_id=first_session_id,
        buyer_id=first_buyer_id,
        offer_id=first_offer.id,
        expected_policy_version=1,
        reason="等待卖家确认",
        expires_at=datetime.now() + timedelta(hours=1),
    )

    cancelled = service.cancel_request(
        session_id=first_session_id,
        buyer_id=first_buyer_id,
        approval_id=first.id,
    )
    cancelled_again = service.cancel_request(
        session_id=first_session_id,
        buyer_id=first_buyer_id,
        approval_id=first.id,
    )

    assert cancelled.status is ApprovalStatus.CANCELLED
    assert cancelled_again.status is ApprovalStatus.CANCELLED
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, first_session_id)
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE

    second_session_id, second_buyer_id, second_offer = create_buyer_offer(
        service_session_factory
    )
    expires_at = datetime.now() + timedelta(minutes=30)
    second = service.create_request(
        session_id=second_session_id,
        buyer_id=second_buyer_id,
        offer_id=second_offer.id,
        expected_policy_version=1,
        reason="短时审批",
        expires_at=expires_at,
    )
    with pytest.raises(ApprovalNotExpiredError):
        service.expire_request(approval_id=second.id)

    expired = service.expire_request(
        approval_id=second.id,
        now=expires_at + timedelta(seconds=1),
    )
    expired_again = service.expire_request(
        approval_id=second.id,
        now=expires_at + timedelta(seconds=1),
    )

    assert expired.status is ApprovalStatus.EXPIRED
    assert expired_again.status is ApprovalStatus.EXPIRED
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, second_session_id)
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE


def test_approval_rejects_wrong_zone_offer_state_and_policy_version(
    service_session_factory: sessionmaker[Session],
) -> None:
    service = ApprovalService(service_session_factory)

    automatic_session, automatic_buyer, automatic_offer = create_buyer_offer(
        service_session_factory,
        price="2900.00",
    )
    with pytest.raises(ApprovalNotAuthorizedError):
        service.create_request(
            session_id=automatic_session,
            buyer_id=automatic_buyer,
            offer_id=automatic_offer.id,
            expected_policy_version=1,
            reason="自动接受区不需要审批",
            expires_at=datetime.now() + timedelta(hours=1),
        )

    prohibited_session, prohibited_buyer, prohibited_offer = create_buyer_offer(
        service_session_factory,
        price="2600.00",
    )
    with pytest.raises(ApprovalNotAuthorizedError):
        service.create_request(
            session_id=prohibited_session,
            buyer_id=prohibited_buyer,
            offer_id=prohibited_offer.id,
            expected_policy_version=1,
            reason="禁止区不能审批",
            expires_at=datetime.now() + timedelta(hours=1),
        )

    stale_session, stale_buyer, stale_offer = create_buyer_offer(
        service_session_factory
    )
    with service_session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, stale_session)
        assert negotiation is not None
        policy = db.scalar(
            select(SellerPolicy).where(
                SellerPolicy.product_id == negotiation.product_id
            )
        )
        assert policy is not None
        policy.version += 1
    with pytest.raises(ApprovalConflictError, match="规则版本"):
        service.create_request(
            session_id=stale_session,
            buyer_id=stale_buyer,
            offer_id=stale_offer.id,
            expected_policy_version=1,
            reason="旧规则不能审批",
            expires_at=datetime.now() + timedelta(hours=1),
        )

    old_session, old_buyer, old_offer = create_buyer_offer(service_session_factory)
    NegotiationService(service_session_factory).record_buyer_offer(
        session_id=old_session,
        buyer_id=old_buyer,
        terms=buyer_terms("2810.00"),
    )
    with pytest.raises(ApprovalConflictError, match="当前有效报价"):
        service.create_request(
            session_id=old_session,
            buyer_id=old_buyer,
            offer_id=old_offer.id,
            expected_policy_version=1,
            reason="旧报价不能审批",
            expires_at=datetime.now() + timedelta(hours=1),
        )

    expired_session, expired_buyer, expired_offer = create_buyer_offer(
        service_session_factory
    )
    with service_session_factory() as db, db.begin():
        stored_offer = db.get(Offer, expired_offer.id)
        assert stored_offer is not None
        stored_offer.expires_at = datetime.now() - timedelta(seconds=1)
    with pytest.raises(ApprovalConflictError, match="报价已经过期"):
        service.create_request(
            session_id=expired_session,
            buyer_id=expired_buyer,
            offer_id=expired_offer.id,
            expected_policy_version=1,
            reason="过期报价不能审批",
            expires_at=datetime.now() + timedelta(hours=1),
        )

    terms_session, terms_buyer, terms_offer = create_buyer_offer(
        service_session_factory,
        additional_terms={"ship_by": "today"},
    )
    with pytest.raises(ApprovalNotAuthorizedError):
        service.create_request(
            session_id=terms_session,
            buyer_id=terms_buyer,
            offer_id=terms_offer.id,
            expected_policy_version=1,
            reason="不可校验条件不能审批",
            expires_at=datetime.now() + timedelta(hours=1),
        )


def test_seller_lists_and_approves_owned_pending_request_idempotently(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, offer = create_buyer_offer(service_session_factory)
    service = ApprovalService(service_session_factory)
    created = service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="等待卖家确认",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id

    listed = service.list_for_seller(seller_id=seller_id)
    queried = service.get_for_seller(
        seller_id=seller_id,
        approval_id=created.id,
    )
    approved = service.approve_request(
        seller_id=seller_id,
        approval_id=created.id,
        request_id="approve-request-001",
        comment="同意该报价",
    )
    repeated = service.approve_request(
        seller_id=seller_id,
        approval_id=created.id,
        request_id="approve-request-001",
        comment="重复请求不得覆盖原意见",
    )

    assert [item.id for item in listed] == [created.id]
    assert queried.offer.price == Decimal("2800.00")
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.seller_comment == "同意该报价"
    assert approved.reviewed_at is not None
    assert approved.followup_status is ApprovalFollowupStatus.PENDING
    assert approved.followup_request_id == "approve-request-001"
    assert repeated == approved
    with pytest.raises(ApprovalConflictError):
        service.approve_request(
            seller_id=seller_id,
            approval_id=created.id,
            request_id="approve-request-002",
        )
    with pytest.raises(ApprovalNotFoundError):
        service.get_for_seller(
            seller_id="another-seller",
            approval_id=created.id,
        )
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        stored_offer = db.get(Offer, offer.id)
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.WAITING_APPROVAL
        assert stored_offer is not None
        assert stored_offer.status is OfferStatus.PROPOSED


def test_seller_rejection_restores_session_and_enqueues_followup(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, offer = create_buyer_offer(service_session_factory)
    service = ApprovalService(service_session_factory)
    created = service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="等待卖家确认",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id

    rejected = service.reject_request(
        seller_id=seller_id,
        approval_id=created.id,
        request_id="reject-request-001",
        comment="价格暂不合适",
    )
    repeated = service.reject_request(
        seller_id=seller_id,
        approval_id=created.id,
        request_id="reject-request-001",
    )

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.followup_status is ApprovalFollowupStatus.PENDING
    assert repeated == rejected
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        stored_offer = db.get(Offer, offer.id)
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE
        assert stored_offer is not None
        assert stored_offer.status is OfferStatus.REJECTED


def test_seller_review_invalidates_stale_policy_before_returning_conflict(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, offer = create_buyer_offer(service_session_factory)
    service = ApprovalService(service_session_factory)
    created = service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="等待卖家确认",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with service_session_factory() as db, db.begin():
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
        policy = db.scalar(
            select(SellerPolicy).where(
                SellerPolicy.product_id == negotiation.product_id
            )
        )
        assert policy is not None
        policy.version += 1

    with pytest.raises(ApprovalConflictError, match="规则版本"):
        service.approve_request(
            seller_id=seller_id,
            approval_id=created.id,
            request_id="approve-stale-policy-001",
        )

    with service_session_factory() as db:
        approval = db.get(ApprovalRequest, created.id)
        negotiation = db.get(NegotiationSession, session_id)
        assert approval is not None
        assert approval.status is ApprovalStatus.EXPIRED
        assert approval.followup_status is None
        assert negotiation is not None
        assert negotiation.status is NegotiationStatus.ACTIVE


def test_seller_list_expires_due_request_and_restores_session(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, offer = create_buyer_offer(service_session_factory)
    service = ApprovalService(service_session_factory)
    created = service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="即将过期的审批",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with service_session_factory() as db, db.begin():
        approval = db.get(ApprovalRequest, created.id)
        negotiation = db.get(NegotiationSession, session_id)
        assert approval is not None
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
        approval.expires_at = datetime.now() - timedelta(seconds=1)

    pending = service.list_for_seller(
        seller_id=seller_id,
        approval_status=ApprovalStatus.PENDING,
    )
    detail = service.get_for_seller(
        seller_id=seller_id,
        approval_id=created.id,
    )

    assert pending == ()
    assert detail.status is ApprovalStatus.EXPIRED
    assert detail.session_status is NegotiationStatus.ACTIVE


def test_concurrent_requests_create_only_one_pending_approval(
    mysql_engine: Engine,
) -> None:
    committed_factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session_id, buyer_id, offer = create_buyer_offer(committed_factory)
    expires_at = datetime.now() + timedelta(hours=1)
    barrier = Barrier(2)

    def submit() -> int:
        barrier.wait()
        approval = ApprovalService(committed_factory).create_request(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=offer.id,
            expected_policy_version=1,
            reason="并发审批测试",
            expires_at=expires_at,
        )
        return approval.id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            approval_ids = tuple(executor.map(lambda _: submit(), range(2)))

        assert approval_ids[0] == approval_ids[1]
        with committed_factory() as db:
            pending_count = db.scalar(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(
                    ApprovalRequest.session_id == session_id,
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                )
            )
            assert pending_count == 1
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
