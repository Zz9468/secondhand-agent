import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    NegotiationSession,
    NegotiationStatus,
    NegotiationStyle,
    Offer,
    OfferProposer,
    OfferStatus,
    Product,
    ProductStatus,
    SellerPolicy,
)
from app.db.models import ShippingPayer as StoredShippingPayer
from app.services.errors import (
    InvalidNegotiationStateError,
    InvalidOfferTermsError,
    NegotiationNotFoundError,
    OfferConflictError,
    OfferNotAuthorizedError,
    OfferNotFoundError,
    PricingPolicyNotFoundError,
    ProductUnavailableError,
)
from app.services.pricing_service import (
    OfferTerms,
    PriceZone,
    PricingError,
    PricingPolicy,
    PricingService,
    ShippingPayer,
)


@dataclass(frozen=True, slots=True)
class OfferAuthorization:
    zone: PriceZone
    conditions_valid: bool
    can_accept_automatically: bool
    can_submit_counter_offer: bool
    can_request_approval: bool
    is_acceptance_prohibited: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class OfferSnapshot:
    id: int
    proposer: OfferProposer
    price: Decimal
    shipping_paid_by: StoredShippingPayer
    shipping_cost: Decimal | None
    seller_borne_discount: Decimal
    additional_terms: dict[str, object]
    status: OfferStatus
    expires_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NegotiationState:
    id: int
    product_id: int
    status: NegotiationStatus
    current_offer_id: int | None
    round_count: int
    version: int
    negotiation_style: NegotiationStyle
    max_rounds: int
    recent_offers: tuple[OfferSnapshot, ...]


class NegotiationService:
    """协调协商状态、报价持久化、事务锁和价格规则校验。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        pricing_service: PricingService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._pricing_service = pricing_service or PricingService()

    def create_or_get_active_session(
        self,
        *,
        product_id: int,
        buyer_id: str,
    ) -> tuple[int, bool]:
        """为可信访客创建或复用当前商品的可协商会话。"""

        with self._session_factory() as db, db.begin():
            product = db.scalar(
                select(Product)
                .where(Product.id == product_id)
                .with_for_update()
            )
            if product is None or product.status is not ProductStatus.AVAILABLE:
                raise ProductUnavailableError("商品不存在或当前不可协商")
            self._get_policy(db, product_id, for_update=True)

            existing = db.scalar(
                select(NegotiationSession)
                .where(
                    NegotiationSession.product_id == product_id,
                    NegotiationSession.buyer_id == buyer_id,
                    NegotiationSession.status.in_(
                        (
                            NegotiationStatus.ACTIVE,
                            NegotiationStatus.WAITING_APPROVAL,
                        )
                    ),
                )
                .order_by(NegotiationSession.id.desc())
                .limit(1)
            )
            if existing is not None:
                return existing.id, False

            negotiation = NegotiationSession(
                product_id=product_id,
                buyer_id=buyer_id,
                status=NegotiationStatus.ACTIVE,
                round_count=0,
                version=1,
            )
            db.add(negotiation)
            db.flush()
            return negotiation.id, True

    def get_state(self, *, session_id: int, buyer_id: str) -> NegotiationState:
        with self._session_factory() as db:
            negotiation = self._get_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
            )
            policy = self._get_policy(db, negotiation.product_id)
            offers = list(
                db.scalars(
                    select(Offer)
                    .where(Offer.session_id == negotiation.id)
                    .order_by(Offer.id.desc())
                    .limit(10)
                )
            )
            offers.reverse()
            return NegotiationState(
                id=negotiation.id,
                product_id=negotiation.product_id,
                status=negotiation.status,
                current_offer_id=negotiation.current_offer_id,
                round_count=negotiation.round_count,
                version=negotiation.version,
                negotiation_style=policy.negotiation_style,
                max_rounds=policy.max_rounds,
                recent_offers=tuple(self._snapshot(offer) for offer in offers),
            )

    def evaluate_offer(
        self,
        *,
        session_id: int,
        buyer_id: str,
        terms: OfferTerms,
        additional_terms: Mapping[str, object] | None = None,
    ) -> OfferAuthorization:
        with self._session_factory() as db:
            negotiation = self._get_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
            )
            self._require_negotiable(db, negotiation)
            policy = self._get_policy(db, negotiation.product_id)
            return self._authorize(
                terms=terms,
                policy=policy,
                additional_terms=additional_terms,
            )

    def record_buyer_offer(
        self,
        *,
        session_id: int,
        buyer_id: str,
        terms: OfferTerms,
        additional_terms: Mapping[str, object] | None = None,
        expires_at: datetime | None = None,
    ) -> OfferSnapshot:
        """记录买家报价；低价可以买家提出，但不会因此获得接受授权。"""

        with self._session_factory() as db, db.begin():
            return self.record_buyer_offer_in_transaction(
                db=db,
                session_id=session_id,
                buyer_id=buyer_id,
                terms=terms,
                additional_terms=additional_terms,
                expires_at=expires_at,
            )

    def record_buyer_offer_in_transaction(
        self,
        *,
        db: Session,
        session_id: int,
        buyer_id: str,
        terms: OfferTerms,
        additional_terms: Mapping[str, object] | None = None,
        expires_at: datetime | None = None,
    ) -> OfferSnapshot:
        """在调用方事务内记录买家报价，用于与买家消息原子写入。"""

        stored_terms = self._validated_additional_terms(additional_terms)
        self._validate_expiration(expires_at)
        negotiation = self._get_negotiation(
            db,
            session_id=session_id,
            buyer_id=buyer_id,
            for_update=True,
        )
        self._require_negotiable(db, negotiation, for_update=True)
        policy = self._get_policy(db, negotiation.product_id, for_update=True)
        if negotiation.round_count >= policy.max_rounds:
            raise InvalidNegotiationStateError("当前会话已达到最大议价轮次")
        # 买家可以提出任意价格，但成本必须已知且能够安全计算。
        self._authorize(
            terms=terms,
            policy=policy,
            additional_terms=stored_terms,
        )
        self._supersede_current_offer(
            db,
            negotiation=negotiation,
            incoming_proposer=OfferProposer.BUYER,
        )
        offer = self._new_offer(
            negotiation=negotiation,
            proposer=OfferProposer.BUYER,
            terms=terms,
            additional_terms=stored_terms,
            expires_at=expires_at,
        )
        db.add(offer)
        negotiation.current_offer = offer
        negotiation.round_count += 1
        negotiation.version += 1
        db.flush()
        db.refresh(offer)
        return self._snapshot(offer)

    def submit_counter_offer(
        self,
        *,
        session_id: int,
        buyer_id: str,
        terms: OfferTerms,
        additional_terms: Mapping[str, object] | None = None,
        expires_at: datetime | None = None,
        responding_to_offer_id: int | None = None,
    ) -> OfferSnapshot:
        """仅持久化位于数据库最新自动授权区的 Agent 正式还价。"""

        stored_terms = self._validated_additional_terms(additional_terms)
        self._validate_expiration(expires_at)
        with self._session_factory() as db, db.begin():
            negotiation = self._get_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            self._require_negotiable(db, negotiation, for_update=True)
            policy = self._get_policy(db, negotiation.product_id, for_update=True)
            current_offer = self._get_current_offer(db, negotiation)
            if negotiation.round_count >= policy.max_rounds and (
                responding_to_offer_id is None
                or current_offer is None
                or current_offer.id != responding_to_offer_id
                or current_offer.proposer is not OfferProposer.BUYER
            ):
                raise InvalidNegotiationStateError("当前会话已达到最大议价轮次")
            authorization = self._authorize(
                terms=terms,
                policy=policy,
                additional_terms=stored_terms,
            )
            if not authorization.can_submit_counter_offer:
                raise OfferNotAuthorizedError(
                    "该还价不在 Agent 自动授权区，不能写入正式报价"
                )

            if current_offer is not None and current_offer.status is OfferStatus.ACCEPTED:
                raise OfferConflictError("当前买家报价已被接受，不能再主动还价")

            self._supersede_current_offer(
                db,
                negotiation=negotiation,
                incoming_proposer=OfferProposer.AGENT,
            )
            offer = self._new_offer(
                negotiation=negotiation,
                proposer=OfferProposer.AGENT,
                terms=terms,
                additional_terms=stored_terms,
                expires_at=expires_at,
            )
            db.add(offer)
            negotiation.current_offer = offer
            negotiation.version += 1
            db.flush()
            db.refresh(offer)
            return self._snapshot(offer)

    def accept_offer(
        self,
        *,
        session_id: int,
        buyer_id: str,
        offer_id: int,
    ) -> OfferSnapshot:
        """按数据库最新状态重新校验并接受当前买家报价。"""

        with self._session_factory() as db, db.begin():
            negotiation = self._get_negotiation(
                db,
                session_id=session_id,
                buyer_id=buyer_id,
                for_update=True,
            )
            self._require_negotiable(db, negotiation, for_update=True)
            if negotiation.current_offer_id != offer_id:
                raise OfferConflictError("只能接受当前有效报价")

            offer = db.scalar(
                select(Offer)
                .where(
                    Offer.id == offer_id,
                    Offer.session_id == negotiation.id,
                )
                .with_for_update()
            )
            if offer is None:
                raise OfferNotFoundError("报价不存在")
            if offer.proposer is not OfferProposer.BUYER:
                raise OfferConflictError("只能接受由买家提出的报价")
            if offer.status is not OfferStatus.PROPOSED:
                raise OfferConflictError("报价已失效或已处理")
            if self._is_expired(offer.expires_at):
                raise OfferConflictError("报价已经过期")

            policy = self._get_policy(db, negotiation.product_id, for_update=True)
            authorization = self._authorize(
                terms=self._terms_from_offer(offer),
                policy=policy,
                additional_terms=offer.terms,
            )
            if not authorization.can_accept_automatically:
                raise OfferNotAuthorizedError(
                    "当前报价未经自动授权，不能直接接受"
                )

            offer.status = OfferStatus.ACCEPTED
            negotiation.version += 1
            db.flush()
            return self._snapshot(offer)

    def authorize_stored_offer(
        self,
        *,
        offer: Offer,
        policy: SellerPolicy,
    ) -> OfferAuthorization:
        """使用统一规则重新评估数据库中的不可变报价快照。"""

        return self._authorize(
            terms=self._terms_from_offer(offer),
            policy=policy,
            additional_terms=offer.terms,
        )

    @staticmethod
    def _get_negotiation(
        db: Session,
        *,
        session_id: int,
        buyer_id: str,
        for_update: bool = False,
    ) -> NegotiationSession:
        statement = select(NegotiationSession).where(
            NegotiationSession.id == session_id,
            NegotiationSession.buyer_id == buyer_id,
        )
        if for_update:
            statement = statement.with_for_update()
        negotiation = db.scalar(statement)
        if negotiation is None:
            raise NegotiationNotFoundError("协商会话不存在或当前买家无权访问")
        return negotiation

    @staticmethod
    def _get_policy(
        db: Session,
        product_id: int,
        *,
        for_update: bool = False,
    ) -> SellerPolicy:
        statement = select(SellerPolicy).where(SellerPolicy.product_id == product_id)
        if for_update:
            statement = statement.with_for_update()
        policy = db.scalar(statement)
        if policy is None:
            raise PricingPolicyNotFoundError("商品尚未配置卖家规则")
        return policy

    @staticmethod
    def _require_negotiable(
        db: Session,
        negotiation: NegotiationSession,
        *,
        for_update: bool = False,
    ) -> None:
        if negotiation.status is not NegotiationStatus.ACTIVE:
            raise InvalidNegotiationStateError("当前会话状态不允许继续议价")
        product = db.get(Product, negotiation.product_id, with_for_update=for_update)
        if product is None or product.status is not ProductStatus.AVAILABLE:
            raise ProductUnavailableError("商品当前不可协商")

    def _authorize(
        self,
        *,
        terms: OfferTerms,
        policy: SellerPolicy,
        additional_terms: Mapping[str, object] | None = None,
    ) -> OfferAuthorization:
        try:
            evaluation = self._pricing_service.evaluate(
                terms=terms,
                policy=PricingPolicy(
                    minimum_net_price=policy.minimum_net_price,
                    auto_accept_threshold=policy.auto_accept_threshold,
                ),
            )
        except PricingError as exc:
            raise InvalidOfferTermsError("交易条件无法安全计算") from exc

        conditions_valid = self._additional_terms_are_authorized(additional_terms)
        if not conditions_valid:
            return OfferAuthorization(
                zone=evaluation.zone,
                conditions_valid=False,
                can_accept_automatically=False,
                can_submit_counter_offer=False,
                can_request_approval=False,
                is_acceptance_prohibited=True,
                reason_code="UNSUPPORTED_ADDITIONAL_TERMS",
            )

        reason_codes = {
            PriceZone.AUTO_ACCEPT: "AUTO_AUTHORIZED",
            PriceZone.APPROVAL_REQUIRED: "SELLER_APPROVAL_REQUIRED",
            PriceZone.PROHIBITED: "BELOW_MINIMUM_NET_INCOME",
        }
        return OfferAuthorization(
            zone=evaluation.zone,
            conditions_valid=True,
            can_accept_automatically=evaluation.can_accept_automatically,
            can_submit_counter_offer=evaluation.can_accept_automatically,
            can_request_approval=evaluation.can_request_approval,
            is_acceptance_prohibited=evaluation.is_acceptance_prohibited,
            reason_code=reason_codes[evaluation.zone],
        )

    @staticmethod
    def _additional_terms_are_authorized(
        additional_terms: Mapping[str, object] | None,
    ) -> bool:
        """V1 只自动承诺可验证的配送方式，其他条件留待卖家确认。"""

        if not additional_terms:
            return True
        if set(additional_terms) != {"delivery_method"}:
            return False
        return additional_terms["delivery_method"] in {"shipping", "pickup"}

    @staticmethod
    def _supersede_current_offer(
        db: Session,
        *,
        negotiation: NegotiationSession,
        incoming_proposer: OfferProposer,
    ) -> None:
        if negotiation.current_offer_id is None:
            return
        current = db.get(Offer, negotiation.current_offer_id, with_for_update=True)
        if current is None or current.status is not OfferStatus.PROPOSED:
            return
        current.status = (
            OfferStatus.WITHDRAWN
            if current.proposer is incoming_proposer
            else OfferStatus.REJECTED
        )

    @staticmethod
    def _get_current_offer(
        db: Session,
        negotiation: NegotiationSession,
    ) -> Offer | None:
        if negotiation.current_offer_id is None:
            return None
        return db.get(Offer, negotiation.current_offer_id, with_for_update=True)

    @staticmethod
    def _new_offer(
        *,
        negotiation: NegotiationSession,
        proposer: OfferProposer,
        terms: OfferTerms,
        additional_terms: dict[str, object],
        expires_at: datetime | None,
    ) -> Offer:
        if terms.seller_borne_discount is None:
            raise InvalidOfferTermsError("卖家承担的优惠金额必须明确")
        return Offer(
            session_id=negotiation.id,
            proposer=proposer,
            price=terms.buyer_payment,
            shipping_paid_by=StoredShippingPayer(terms.shipping_paid_by.value),
            shipping_cost=terms.shipping_cost,
            seller_borne_discount=terms.seller_borne_discount,
            terms=additional_terms,
            status=OfferStatus.PROPOSED,
            expires_at=expires_at,
        )

    @staticmethod
    def _terms_from_offer(offer: Offer) -> OfferTerms:
        return OfferTerms(
            buyer_payment=offer.price,
            shipping_paid_by=ShippingPayer(offer.shipping_paid_by.value),
            shipping_cost=offer.shipping_cost,
            seller_borne_discount=offer.seller_borne_discount,
        )

    @staticmethod
    def _validated_additional_terms(
        additional_terms: Mapping[str, object] | None,
    ) -> dict[str, object]:
        stored_terms = dict(additional_terms or {})
        try:
            json.dumps(stored_terms, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InvalidOfferTermsError("附加交易条件必须可以序列化为 JSON") from exc
        return stored_terms

    @staticmethod
    def _validate_expiration(expires_at: datetime | None) -> None:
        if NegotiationService._is_expired(expires_at):
            raise InvalidOfferTermsError("报价失效时间必须晚于当前时间")

    @staticmethod
    def _is_expired(expires_at: datetime | None) -> bool:
        if expires_at is None:
            return False
        now = datetime.now(tz=expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
        return expires_at <= now

    @staticmethod
    def _snapshot(offer: Offer) -> OfferSnapshot:
        return OfferSnapshot(
            id=offer.id,
            proposer=offer.proposer,
            price=offer.price,
            shipping_paid_by=offer.shipping_paid_by,
            shipping_cost=offer.shipping_cost,
            seller_borne_discount=offer.seller_borne_discount,
            additional_terms=dict(offer.terms),
            status=offer.status,
            expires_at=offer.expires_at,
            created_at=offer.created_at,
        )
