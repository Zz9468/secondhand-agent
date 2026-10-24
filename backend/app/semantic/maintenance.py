"""Seller-owned evidence updates and buyer notifications without executing old proposals."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Message, MessageRole, NegotiationSession, Product
from app.db.models.graph import PublicInteractionContext
from app.db.models.semantic import OfferDraft, ProductFactValue, ShippingCostRule, VerificationItem
from app.runtime.contracts import Conflict, utcnow
from app.semantic.contracts import Candidate
from app.semantic.evidence import assess, facts_for, normalize_fact
from app.semantic.service import usable_draft
from app.services.agent_message_service import AgentMessageService


def owned_product(db, pid, seller, expected=None):
    product = db.get(Product, pid, with_for_update=True)
    if not product or product.seller_id != seller:
        raise Conflict("商品不存在或无权访问")
    if expected is not None and product.product_version != expected:
        raise Conflict("商品资料已更新，请刷新后重试")
    return product


class EvidenceMaintenance:
    def __init__(self, factory, *, clock=utcnow):
        self.factory, self.clock = factory, clock

    def inspect(self, pid, seller):
        with self.factory() as db, db.begin():
            product = owned_product(db, pid, seller)
            facts = facts_for(db, pid)
            rules = list(
                db.scalars(select(ShippingCostRule).where(ShippingCostRule.product_id == pid))
            )
            items = list(
                db.scalars(select(VerificationItem).where(VerificationItem.product_id == pid))
            )
            return {
                "product_version": product.product_version,
                "facts": {
                    key: {
                        "value": row.value,
                        "version": row.version,
                        "source": row.source,
                        "confirmed_at": row.confirmed_at,
                    }
                    for key, row in facts.items()
                },
                "rules": [
                    {
                        "id": r.id,
                        "region": r.region,
                        "shipping_cost": str(r.shipping_cost),
                        "discount": str(r.discount),
                        "version": r.version,
                        "active": r.active,
                        "expires_at": r.expires_at,
                    }
                    for r in rules
                ],
                "verifications": [
                    {"id": v.id, "draft_id": v.draft_id, "status": v.status, "reasons": v.reasons}
                    for v in items
                ],
            }

    def set_fact(self, pid, seller, attribute, value, expected):
        normalized = normalize_fact(attribute, value)
        with self.factory() as db, db.begin():
            product = owned_product(db, pid, seller, expected)
            product.product_version += 1
            db.add(
                ProductFactValue(
                    id=uuid4().hex,
                    product_id=pid,
                    attribute=attribute,
                    value=normalized,
                    version=product.product_version,
                    confirmed_by=seller,
                    confirmed_at=self.clock(),
                )
            )
        self.recheck(pid, seller)
        return self.inspect(pid, seller)

    def set_rule(self, pid, seller, data):
        with self.factory() as db, db.begin():
            product = owned_product(db, pid, seller, data.expected_version)
            if data.expires_at <= self.clock() and data.active:
                raise Conflict("有效运费规则必须设置未来失效时间")
            rule = db.get(ShippingCostRule, data.rule_id) if data.rule_id else None
            if data.rule_id and (not rule or rule.product_id != pid):
                raise Conflict("规则不存在或无权访问")
            if rule is None:
                rule = ShippingCostRule(id=uuid4().hex, product_id=pid, delivery="shipping")
                db.add(rule)
            product.product_version += 1
            rule.region, rule.shipping_cost, rule.discount = (
                data.region,
                data.shipping_cost,
                data.discount,
            )
            rule.version, rule.active = product.product_version, data.active
            rule.expires_at, rule.confirmed_at = data.expires_at, self.clock()
            rule.confirmed_by = seller
        self.recheck(pid, seller)
        return self.inspect(pid, seller)

    def recheck(self, pid, seller):
        with self.factory() as db:
            owned_product(db, pid, seller)
            ids = list(
                db.scalars(
                    select(VerificationItem.id).where(
                        VerificationItem.product_id == pid,
                        VerificationItem.status.in_(["OPEN", "READY"]),
                    )
                )
            )
        for key in ids:
            with self.factory() as db, db.begin():
                item = db.get(VerificationItem, key)
                draft = db.get(OfferDraft, item.draft_id)
                session = db.get(NegotiationSession, draft.session_id, with_for_update=True)
                product = owned_product(db, pid, seller)
                now = self.clock()
                if not usable_draft(draft, session, now, draft.version):
                    item.status = "STALE"
                    continue
                missing, _, _, _ = assess(
                    db, product, Candidate.model_validate(draft.candidate), now
                )
                item.reasons, draft.missing = missing, missing
                item.status = draft.status = "PENDING" if missing else "READY"
                if missing:
                    item.status = "OPEN"
                    continue
                if item.notified_version == product.product_version:
                    continue
                source = f"draft-ready:{draft.id}:{product.product_version}"
                context_id = uuid4().hex
                refs = [{"id": draft.id, "version": draft.version}]
                message = Message(
                    session_id=session.id,
                    role=MessageRole.SYSTEM,
                    source_id=source,
                    request_id=source,
                    public_seq=session.next_public_seq,
                    content="卖家已补充此草案所需资料。原报价尚未提交；请查看原条件并明确回复继续。",
                    card={
                        "kind": "drafts",
                        "context_id": context_id,
                        "can_confirm": False,
                        "drafts": [
                            {
                                "draft_id": draft.id,
                                "draft_version": draft.version,
                                "candidate": draft.candidate,
                                "missing": [],
                            }
                        ],
                    },
                )
                session.next_public_seq += 1
                db.add(message)
                db.flush()
                db.add(
                    PublicInteractionContext(
                        id=context_id,
                        session_id=session.id,
                        buyer_id=session.buyer_id,
                        message_id=message.id,
                        public_seq=message.public_seq,
                        terms_hash="drafts-v1",
                        draft_refs=refs,
                        business_version=session.version,
                        expires_at=min(draft.expires_at, now + timedelta(minutes=30)),
                    )
                )
                AgentMessageService.project(db, message, kind="DRAFT_READY")
                item.notified_version = product.product_version
