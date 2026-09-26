from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.agent.approval_followup import (
    ApprovalFollowupDraft,
    ApprovalFollowupRequest,
)
from app.db.models import (
    ApprovalFollowupStatus,
    ApprovalRequest,
    ApprovalStatus,
    ConfirmationSource,
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
from app.services.chat_service import ChatService
from app.services.errors import (
    MessageConflictError,
    NegotiationLifecycleConflictError,
    NegotiationNotFoundError,
)
from app.services.intent_service import IntentService
from app.services.negotiation_service import NegotiationService, OfferSnapshot
from app.services.pricing_service import OfferTerms, ShippingPayer
from app.workers.approval_processor import ApprovalProcessor
from tests.fakes import RoutingDecisionProvider, demo_negotiation_decision
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


class SuccessfulFollowupProvider:
    def draft(self, request: ApprovalFollowupRequest) -> ApprovalFollowupDraft:
        return ApprovalFollowupDraft(
            acknowledged_event=request.event,
            reason="测试审批通知",
            reply="候选文案不会直接发送",
        )


def _terms(price: str) -> OfferTerms:
    return OfferTerms(
        buyer_payment=Decimal(price),
        shipping_paid_by=ShippingPayer.BUYER,
    )


def _add_formal_agent_message(
    session_factory: sessionmaker[Session],
    *,
    session_id: int,
    offer_id: int,
    outcome: str,
) -> None:
    with session_factory() as db, db.begin():
        db.add(
            Message(
                session_id=session_id,
                role=MessageRole.AGENT,
                content="测试正式回复",
                request_id=f"formal-{offer_id}-{outcome.lower()}",
                agent_outcome=outcome,
                formal_offer_id=offer_id,
            )
        )


def _create_agent_counter(
    session_factory: sessionmaker[Session],
) -> tuple[int, str, OfferSnapshot]:
    session_id, buyer_id = create_negotiation(session_factory)
    service = NegotiationService(session_factory)
    buyer_offer = service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=_terms("2750.00"),
    )
    counter = service.submit_counter_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=_terms("2850.00"),
        additional_terms={"delivery_method": "shipping"},
        responding_to_offer_id=buyer_offer.id,
    )
    _add_formal_agent_message(
        session_factory,
        session_id=session_id,
        offer_id=counter.id,
        outcome="COUNTER_OFFERED",
    )
    return session_id, buyer_id, counter


def _create_auto_accepted_buyer_offer(
    session_factory: sessionmaker[Session],
) -> tuple[int, str, OfferSnapshot]:
    session_id, buyer_id = create_negotiation(session_factory)
    service = NegotiationService(session_factory)
    offer = service.record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=_terms("2900.00"),
        additional_terms={"delivery_method": "pickup"},
    )
    accepted = service.accept_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
    )
    _add_formal_agent_message(
        session_factory,
        session_id=session_id,
        offer_id=offer.id,
        outcome="OFFER_ACCEPTED",
    )
    return session_id, buyer_id, accepted


def _create_seller_approved_offer(
    session_factory: sessionmaker[Session],
) -> tuple[int, str, OfferSnapshot, int]:
    session_id, buyer_id = create_negotiation(session_factory)
    offer = NegotiationService(session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=_terms("2800.00"),
        additional_terms={"delivery_method": "shipping"},
    )
    approval_service = ApprovalService(session_factory)
    approval = approval_service.create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="卖家审批区报价",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    with session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        seller_id = product.seller_id
    approval_service.approve_request(
        seller_id=seller_id,
        approval_id=approval.id,
        request_id=f"approve-intent-{approval.id}",
    )
    result = ApprovalProcessor(
        session_factory,
        SuccessfulFollowupProvider(),
    ).process_next()
    assert result is not None
    assert result.status is ApprovalFollowupStatus.SENT
    with session_factory() as db:
        stored_offer = db.get(Offer, offer.id)
        assert stored_offer is not None
        snapshot = NegotiationService._snapshot(stored_offer)
    return session_id, buyer_id, snapshot, approval.id


def test_confirm_agent_counter_persists_intent_and_replays_without_duplicate(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, offer = _create_agent_counter(service_session_factory)
    service = IntentService(service_session_factory)

    first = service.confirm_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        request_id="confirm-agent-counter-001",
    )
    replay = service.confirm_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        request_id="confirm-agent-counter-retry",
    )

    assert first.status is NegotiationStatus.AGREED
    assert first.confirmation_source is ConfirmationSource.AGENT_COUNTER
    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.system_message.id == first.system_message.id
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        stored_offer = db.get(Offer, offer.id)
        system_count = db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.session_id == session_id,
                Message.role == MessageRole.SYSTEM,
            )
        )
        assert negotiation is not None
        assert negotiation.confirmed_offer_id == offer.id
        assert negotiation.confirmation_request_id == "confirm-agent-counter-001"
        assert negotiation.confirmation_source is ConfirmationSource.AGENT_COUNTER
        assert stored_offer is not None
        assert stored_offer.status is OfferStatus.ACCEPTED
        assert system_count == 1
    with pytest.raises(MessageConflictError, match="已经结束"):
        ChatService(
            service_session_factory,
            RoutingDecisionProvider(demo_negotiation_decision),
        ).send_buyer_message(
            session_id=session_id,
            buyer_id=buyer_id,
            request_id="message-after-agreed-001",
            content="确认后继续还价",
        )


def test_confirm_supports_auto_accept_and_completed_seller_approval(
    service_session_factory: sessionmaker[Session],
) -> None:
    auto_session_id, auto_buyer_id, auto_offer = _create_auto_accepted_buyer_offer(
        service_session_factory
    )
    approved_session_id, approved_buyer_id, approved_offer, _ = (
        _create_seller_approved_offer(service_session_factory)
    )
    service = IntentService(service_session_factory)

    automatic = service.confirm_offer(
        session_id=auto_session_id,
        buyer_id=auto_buyer_id,
        offer_id=auto_offer.id,
        request_id="confirm-auto-accepted-001",
    )
    approved = service.confirm_offer(
        session_id=approved_session_id,
        buyer_id=approved_buyer_id,
        offer_id=approved_offer.id,
        request_id="confirm-approved-001",
    )

    assert (
        automatic.confirmation_source
        is ConfirmationSource.AUTO_ACCEPTED_BUYER_OFFER
    )
    assert (
        approved.confirmation_source
        is ConfirmationSource.SELLER_APPROVED_BUYER_OFFER
    )


def test_confirm_rejects_wrong_buyer_old_offer_and_missing_formal_reply(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id, counter = _create_agent_counter(service_session_factory)
    service = IntentService(service_session_factory)
    with service_session_factory() as db:
        old_offer = db.scalar(
            select(Offer)
            .where(
                Offer.session_id == session_id,
                Offer.id != counter.id,
            )
            .order_by(Offer.id)
        )
        assert old_offer is not None

    with pytest.raises(NegotiationNotFoundError):
        service.confirm_offer(
            session_id=session_id,
            buyer_id="another-buyer",
            offer_id=counter.id,
            request_id="confirm-wrong-buyer-001",
        )
    with pytest.raises(NegotiationLifecycleConflictError, match="当前有效报价"):
        service.confirm_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=old_offer.id,
            request_id="confirm-old-offer-001",
        )

    missing_session_id, missing_buyer_id = create_negotiation(
        service_session_factory
    )
    negotiation_service = NegotiationService(service_session_factory)
    missing_offer = negotiation_service.record_buyer_offer(
        session_id=missing_session_id,
        buyer_id=missing_buyer_id,
        terms=_terms("2900.00"),
    )
    negotiation_service.accept_offer(
        session_id=missing_session_id,
        buyer_id=missing_buyer_id,
        offer_id=missing_offer.id,
    )
    with pytest.raises(NegotiationLifecycleConflictError, match="自动接受记录"):
        service.confirm_offer(
            session_id=missing_session_id,
            buyer_id=missing_buyer_id,
            offer_id=missing_offer.id,
            request_id="confirm-missing-reply-001",
        )


def test_confirm_rejects_expired_offer_and_stale_approval_policy(
    service_session_factory: sessionmaker[Session],
) -> None:
    expired_session_id, expired_buyer_id, expired_offer = _create_agent_counter(
        service_session_factory
    )
    with service_session_factory() as db, db.begin():
        stored_offer = db.get(Offer, expired_offer.id)
        assert stored_offer is not None
        stored_offer.expires_at = datetime.now() - timedelta(seconds=1)

    service = IntentService(service_session_factory)
    with pytest.raises(NegotiationLifecycleConflictError, match="已经过期"):
        service.confirm_offer(
            session_id=expired_session_id,
            buyer_id=expired_buyer_id,
            offer_id=expired_offer.id,
            request_id="confirm-expired-offer-001",
        )

    session_id, buyer_id, offer, _ = _create_seller_approved_offer(
        service_session_factory
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
    with pytest.raises(NegotiationLifecycleConflictError, match="审批授权"):
        service.confirm_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=offer.id,
            request_id="confirm-stale-approval-001",
        )


def test_close_cancels_pending_approval_and_blocks_future_messages(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, buyer_id = create_negotiation(service_session_factory)
    offer = NegotiationService(service_session_factory).record_buyer_offer(
        session_id=session_id,
        buyer_id=buyer_id,
        terms=_terms("2800.00"),
    )
    approval = ApprovalService(service_session_factory).create_request(
        session_id=session_id,
        buyer_id=buyer_id,
        offer_id=offer.id,
        expected_policy_version=1,
        reason="关闭会话测试",
        expires_at=datetime.now() + timedelta(hours=1),
    )
    service = IntentService(service_session_factory)

    closed = service.close_negotiation(
        session_id=session_id,
        buyer_id=buyer_id,
        request_id="close-negotiation-001",
    )
    replay = service.close_negotiation(
        session_id=session_id,
        buyer_id=buyer_id,
        request_id="close-negotiation-retry",
    )

    assert closed.status is NegotiationStatus.CLOSED
    assert closed.cancelled_approval_id == approval.id
    assert replay.idempotent_replay is True
    assert replay.system_message.id == closed.system_message.id
    with service_session_factory() as db:
        stored_approval = db.get(ApprovalRequest, approval.id)
        assert stored_approval is not None
        assert stored_approval.status is ApprovalStatus.CANCELLED
    with pytest.raises(MessageConflictError, match="已经结束"):
        ChatService(
            service_session_factory,
            RoutingDecisionProvider(demo_negotiation_decision),
        ).send_buyer_message(
            session_id=session_id,
            buyer_id=buyer_id,
            request_id="message-after-close-001",
            content="继续还价",
        )


def test_concurrent_confirmation_writes_one_intent(mysql_engine: Engine) -> None:
    committed_factory = sessionmaker(bind=mysql_engine, expire_on_commit=False)
    session_id, buyer_id, offer = _create_agent_counter(committed_factory)
    barrier = Barrier(2)

    def confirm(index: int) -> bool:
        barrier.wait()
        return IntentService(committed_factory).confirm_offer(
            session_id=session_id,
            buyer_id=buyer_id,
            offer_id=offer.id,
            request_id=f"confirm-concurrent-{index:03d}",
        ).idempotent_replay

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            replay_flags = tuple(executor.map(confirm, range(2)))

        assert sorted(replay_flags) == [False, True]
        with committed_factory() as db:
            system_count = db.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.session_id == session_id,
                    Message.role == MessageRole.SYSTEM,
                )
            )
            negotiation = db.get(NegotiationSession, session_id)
            assert system_count == 1
            assert negotiation is not None
            assert negotiation.status is NegotiationStatus.AGREED
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
        negotiation.confirmed_offer_id = None
        negotiation.confirmed_at = None
        negotiation.confirmation_request_id = None
        negotiation.confirmation_source = None
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
