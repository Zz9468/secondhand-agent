"""P1 可组合业务操作：只使用调用方 Session，不提交、不调用模型。"""

from datetime import timedelta

from sqlalchemy import select

from app.db.models import (
    ApprovalRequest,
    ApprovalStatus,
    ConfirmationSource,
    NegotiationStatus,
    Offer,
    OfferProposer,
    OfferStatus,
    ProductStatus,
)
from app.db.models.graph import DisplayAck, GraphOfferEvidence, PublicInteractionContext
from app.runtime.contracts import Conflict, digest
from app.services.negotiation_service import NegotiationService
from app.services.pricing_service import OfferTerms, PricingPolicy, ShippingPayer, authorize_offer


def product_hash(product):
    return digest(
        {
            "title": product.title,
            "description": product.description,
            "price": str(product.listed_price),
            "status": product.status.value,
        }
    )


def terms_hash(offer):
    return digest(
        {
            "amount": f"{offer.price:.2f}",
            "payer": offer.shipping_paid_by.value,
            "shipping_cost": f"{offer.shipping_cost:.2f}"
            if offer.shipping_cost is not None
            else None,
            "discount": f"{offer.seller_borne_discount:.2f}",
            "terms": offer.terms,
        }
    )


def pricing_terms(terms):
    return OfferTerms(
        terms.amount,
        ShippingPayer(terms.shipping_paid_by),
        terms.shipping_cost,
        terms.seller_discount,
    )


def authorized(terms, policy):
    return authorize_offer(
        terms=pricing_terms(terms),
        policy=PricingPolicy(policy.minimum_net_price, policy.auto_accept_threshold),
        additional_terms=terms.additional_terms,
    )


def basis_valid(db, session, product, policy, offer, now):
    if (
        not offer
        or offer.session_id != session.id
        or session.current_offer_id != offer.id
        or session.status not in {NegotiationStatus.ACTIVE, NegotiationStatus.WAITING_APPROVAL}
        or product.status != ProductStatus.AVAILABLE
        or offer.status not in {OfferStatus.PROPOSED, OfferStatus.ACCEPTED}
        or not offer.expires_at
        or offer.expires_at <= now
    ):
        return False
    proof = db.get(GraphOfferEvidence, offer.id)
    if session.graph_version in {"negotiation-v2", "negotiation-v3"}:
        from app.semantic.evidence import valid_basis

        if not valid_basis(db, product, offer, now):
            return False
    try:
        current_authorization = authorize_offer(
            terms=NegotiationService._terms_from_offer(offer),
            policy=PricingPolicy(policy.minimum_net_price, policy.auto_accept_threshold),
            additional_terms=offer.terms,
        )
    except ValueError:
        return False
    if offer.proposer == OfferProposer.AGENT and not current_authorization.can_submit_counter_offer:
        return False
    if (
        offer.status == OfferStatus.ACCEPTED
        and not current_authorization.can_accept_automatically
        and not db.scalar(select(ApprovalRequest.id).where(ApprovalRequest.offer_id == offer.id))
    ):
        return False
    return bool(
        proof
        and not current_authorization.is_acceptance_prohibited
        and current_authorization.conditions_valid
        and proof.policy_version == policy.version
        and proof.product_hash == product_hash(product)
        and proof.terms_hash == terms_hash(offer)
    )


def valid_offer(db, session, product, policy, offer, now):
    if session.status != NegotiationStatus.ACTIVE or not basis_valid(
        db, session, product, policy, offer, now
    ):
        return False
    authorization = authorize_offer(
        terms=NegotiationService._terms_from_offer(offer),
        policy=PricingPolicy(policy.minimum_net_price, policy.auto_accept_threshold),
        additional_terms=offer.terms,
    )
    if offer.proposer == OfferProposer.AGENT:
        return offer.status == OfferStatus.PROPOSED and authorization.can_submit_counter_offer
    if offer.status != OfferStatus.ACCEPTED:
        return False
    approval = db.scalar(select(ApprovalRequest).where(ApprovalRequest.offer_id == offer.id))
    if approval:
        return bool(
            approval.status == ApprovalStatus.APPROVED
            and approval.effect_status == "APPLIED"
            and approval.policy_version == policy.version
            and approval.reviewed_at
            and approval.reviewed_at < approval.expires_at
        )
    return authorization.can_accept_automatically


def confirmation_expiry(db, offer, now):
    from app.db.models.semantic import CostQuote, SemanticOfferBasis

    expiry = min(offer.expires_at, now + timedelta(minutes=30))
    basis = db.get(SemanticOfferBasis, offer.id)
    if basis:
        expiry = min(expiry, db.get(CostQuote, basis.cost_quote_id).expires_at)
    return expiry


def new_offer_in_transaction(db, session, product, policy, terms, proposer, now):
    offer = NegotiationService._new_offer(
        negotiation=session,
        proposer=proposer,
        terms=pricing_terms(terms),
        additional_terms=dict(terms.additional_terms),
        expires_at=now + timedelta(hours=24),
    )
    db.add(offer)
    db.flush()
    db.add(
        GraphOfferEvidence(
            offer_id=offer.id,
            terms_hash=terms_hash(offer),
            policy_version=policy.version,
            product_hash=product_hash(product),
        )
    )
    return offer


def resolve_offer_in_transaction(
    db, session, product, policy, payload, now, fault, semantic_candidate=None,
    audit_authorized=lambda: None,
):
    """新买家提案、替代、自动接受/审批/合法还价在同一事务内完成。"""
    if product.status != ProductStatus.AVAILABLE:
        raise Conflict("商品当前不可协商")
    authorization = authorized(payload.terms, policy)
    if not authorization.conditions_valid:
        raise Conflict("附加条款尚不支持")
    current = (
        db.get(Offer, session.current_offer_id, with_for_update=True)
        if session.current_offer_id
        else None
    )
    if payload.kind == "REPLACE" and (not current or payload.offer_id != current.id):
        raise Conflict("只能替代当前报价")
    # 不同请求、相同有效买家提案不重复计轮，也不延长有效期。
    candidate = NegotiationService._new_offer(
        negotiation=session,
        proposer=OfferProposer.BUYER,
        terms=pricing_terms(payload.terms),
        additional_terms=payload.terms.additional_terms,
        expires_at=None,
    )
    if (
        current
        and current.proposer == OfferProposer.BUYER
        and current.status in {OfferStatus.PROPOSED, OfferStatus.ACCEPTED}
        and current.expires_at > now
        and terms_hash(current) == terms_hash(candidate)
    ):
        proof = db.get(GraphOfferEvidence, current.id)
        same_semantics = session.graph_version not in {"negotiation-v2", "negotiation-v3"}
        if semantic_candidate is not None:
            from app.db.models.semantic import SemanticOfferBasis
            from app.semantic.contracts import Candidate
            from app.semantic.evidence import valid_basis

            basis = db.get(SemanticOfferBasis, current.id)
            same_semantics = bool(
                basis
                and Candidate.model_validate(basis.candidate) == semantic_candidate
                and valid_basis(db, product, current, now)
            )
        if (
            proof
            and proof.policy_version == policy.version
            and proof.product_hash == product_hash(product)
            and same_semantics
        ):
            audit_authorized()
            approval = db.scalar(
                select(ApprovalRequest).where(ApprovalRequest.offer_id == current.id)
            )
            return {
                "outcome": "OFFER_ACCEPTED"
                if current.status == OfferStatus.ACCEPTED
                else "WAITING_APPROVAL",
                "offer_id": current.id,
                "approval_id": approval.id if approval else None,
                "reused": True,
            }
    if session.round_count >= policy.max_rounds:
        raise Conflict("已达到最大议价轮次")
    if (
        payload.counter_terms
        and not authorized(payload.counter_terms, policy).can_submit_counter_offer
    ):
        raise Conflict("还价候选自身未获自动授权")
    # 价格过低的正式提案仍计轮，但不得取消之前有效的报价或审批。
    rejected = authorization.is_acceptance_prohibited and not payload.counter_terms
    audit_authorized()
    if not rejected:
        from app.services.approval_lifecycle import invalidate_offer

        invalidate_offer(db, session, current, now, "REPLACED")
    buyer_offer = new_offer_in_transaction(
        db,
        session,
        product,
        policy,
        payload.terms,
        OfferProposer.BUYER,
        now,
    )
    fault("offer")
    if not rejected:
        from app.services.approval_lifecycle import supersede

        supersede(db, current, buyer_offer)
    session.round_count += 1
    session.version += 1
    db.flush()
    fault("round")
    result = {"offer_id": buyer_offer.id, "approval_id": None, "reused": False}
    if authorization.can_accept_automatically:
        buyer_offer.status = OfferStatus.ACCEPTED
        session.current_offer_id = buyer_offer.id
        result["outcome"] = "OFFER_ACCEPTED"
    elif authorization.can_request_approval:
        session.current_offer_id = buyer_offer.id
        session.status = NegotiationStatus.WAITING_APPROVAL
        approval = ApprovalRequest(
            session_id=session.id,
            offer_id=buyer_offer.id,
            policy_version=policy.version,
            status=ApprovalStatus.PENDING,
            reason="买家报价需要卖家确认，尚未接受。",
            expires_at=min(now + timedelta(minutes=30), buyer_offer.expires_at),
        )
        db.add(approval)
        db.flush()
        from app.services.approval_lifecycle import start_approval

        start_approval(db, approval)
        result.update(outcome="WAITING_APPROVAL", approval_id=approval.id)
    else:
        buyer_offer.status = OfferStatus.REJECTED
        result["outcome"] = "OFFER_REJECTED"
        if payload.counter_terms:
            counter = new_offer_in_transaction(
                db,
                session,
                product,
                policy,
                payload.counter_terms,
                OfferProposer.AGENT,
                now,
            )
            session.current_offer_id = counter.id
            result.update(
                outcome="COUNTER_OFFERED", offer_id=counter.id, buyer_offer_id=buyer_offer.id
            )
    db.flush()
    fault("resolution")
    return result


def confirm_in_transaction(
    db, session, product, policy, payload, run, now, audit_authorized=lambda: None,
):
    offer = db.get(Offer, payload.offer_id, with_for_update=True)
    ack = (
        db.get(DisplayAck, run.payload.get("display_receipt_id"))
        if run.payload.get("display_receipt_id")
        else None
    )
    context = db.get(PublicInteractionContext, ack.context_id) if ack else None
    if (
        not valid_offer(db, session, product, policy, offer, now)
        or not context
        or ack.buyer_id != session.buyer_id
        or context.buyer_id != session.buyer_id
        or ack.client_view_id != payload.client_view_id
        or context.session_id != session.id
        or context.offer_id != offer.id
        or context.business_version != session.version
        or context.terms_hash != terms_hash(offer)
        or ack.displayed_at > run.received_at
        or context.public_seq > run.seen_public_message_seq
        or context.expires_at <= now
    ):
        raise Conflict("缺少接收前已展示且当前有效的报价确认凭据")
    audit_authorized()
    session.status = NegotiationStatus.AGREED
    session.confirmed_offer_id = offer.id
    session.confirmed_at = now
    session.confirmation_request_id = run.id
    session.confirmation_source = (
        ConfirmationSource.AGENT_COUNTER
        if offer.proposer == OfferProposer.AGENT
        else ConfirmationSource.AUTO_ACCEPTED_BUYER_OFFER
    )
    approval = db.scalar(select(ApprovalRequest).where(ApprovalRequest.offer_id == offer.id))
    if approval and approval.effect_status == "APPLIED":
        session.confirmation_source = ConfirmationSource.SELLER_APPROVED_BUYER_OFFER
    offer.status = OfferStatus.ACCEPTED
    session.version += 1
    return {"outcome": "INTENT_CONFIRMED", "offer_id": offer.id}


def lifecycle_in_transaction(db, session, payload, now, audit_authorized=lambda: None):
    offer = (
        db.get(Offer, session.current_offer_id, with_for_update=True)
        if session.current_offer_id
        else None
    )
    if payload.kind == "WITHDRAW" and (
        not offer
        or offer.id != payload.offer_id
        or offer.proposer != OfferProposer.BUYER
        or offer.status not in {OfferStatus.PROPOSED, OfferStatus.ACCEPTED}
    ):
        raise Conflict("只能撤回当前有效报价")
    audit_authorized()
    from app.services.approval_lifecycle import close_drafts, invalidate_offer

    invalidate_offer(db, session, offer, now, "CLOSED" if payload.kind == "CLOSE" else "WITHDRAWN")
    if payload.kind == "CLOSE":
        close_drafts(db, session)
    session.current_offer_id = None
    session.status = (
        NegotiationStatus.CLOSED if payload.kind == "CLOSE" else NegotiationStatus.ACTIVE
    )
    session.version += 1
    return {
        "outcome": "SESSION_CLOSED" if payload.kind == "CLOSE" else "OFFER_WITHDRAWN",
        "offer_id": offer.id if offer else None,
    }
