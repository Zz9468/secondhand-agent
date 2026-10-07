from datetime import UTC, datetime, timedelta
from decimal import Decimal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict

from app.agent.tools.schemas import AcceptOfferInput, OfferTermsInput, RequestApprovalInput
from app.agent.tools.serialization import authorization_result
from app.services.pricing_service import PricingPolicy, PricingService
from evaluation.schemas import OfferSpec


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationEnvironment:
    """Isolated domain adapter using the production authorization and reply contracts."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.offers = {}
        self.current_offer_id = None
        self.current_turn_offer_id = None
        self.status = "ACTIVE"
        self.version = 1
        self.round_count = 0
        self.confirmed_offer_id = None
        self.approval = None
        self.pricing = PricingService()
        self.policy = PricingPolicy(
            scenario.policy.minimum_net_price, scenario.policy.auto_accept_threshold
        )
        self.commitments = []
        self.violations = []
        self.safety_observations = []
        self.audited_commitments = 0
        self.approval_notifications = []

    def product(self):
        return {
            "ok": True,
            "product": {
                "id": 1,
                **self.scenario.product.model_dump(mode="json"),
                "status": "AVAILABLE",
            },
        }

    def state(self):
        return {
            "ok": True,
            "negotiation": {
                "id": 1,
                "product_id": 1,
                "status": self.status,
                "current_offer_id": self.current_offer_id,
                "confirmed_offer_id": self.confirmed_offer_id,
                "confirmed_at": None,
                "confirmation_source": None,
                "round_count": self.round_count,
                "version": self.version,
                "policy_version": 1,
                "negotiation_style": "BALANCED",
                "max_rounds": self.scenario.policy.max_rounds,
                "recent_offers": list(self.offers.values())[-10:],
            },
        }

    def evaluate(self, **args):
        try:
            terms = OfferTermsInput.model_validate(args)
            return authorization_result(
                self.pricing.authorize(
                    terms=terms.to_domain(),
                    policy=self.policy,
                    additional_terms=terms.additional_terms,
                )
            )
        except ValueError:
            return {"ok": False, "error": {"code": "INVALID_OFFER_TERMS"}}

    def _record(self, spec, proposer):
        if self.status != "ACTIVE" or (
            proposer == "BUYER" and self.round_count >= self.scenario.policy.max_rounds
        ):
            raise ValueError("Negotiation cannot accept another formal offer")
        old = self.offers.get(self.current_offer_id)
        if old and old["status"] == "PROPOSED":
            old["status"] = "WITHDRAWN" if old["proposer"] == proposer else "REJECTED"
        offer_id = len(self.offers) + 1
        offer = {
            "id": offer_id,
            **spec.model_dump(mode="json"),
            "proposer": proposer,
            "status": "PROPOSED",
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
        }
        self.offers[offer_id] = offer
        self.current_offer_id = offer_id
        if proposer == "BUYER":
            self.round_count += 1
        self.version += 1
        return offer

    def buyer_offer(self, spec):
        self.pricing.authorize(
            terms=OfferTermsInput.model_validate(spec.model_dump()).to_domain(),
            policy=self.policy,
            additional_terms=spec.additional_terms,
        )
        if self.status != "ACTIVE":
            raise ValueError("Negotiation cannot accept a buyer offer in its current state")
        self.approval = None
        self.current_turn_offer_id = self._record(spec, "BUYER")["id"]

    def counter(self, **args):
        current = self.offers.get(self.current_offer_id)
        if (current and current["status"] == "ACCEPTED") or (
            self.round_count >= self.scenario.policy.max_rounds
            and self.current_turn_offer_id != self.current_offer_id
        ):
            return {"ok": False, "error": {"code": "OFFER_CONFLICT"}}
        evaluation = self.evaluate(**args)
        if not evaluation.get("authorization", {}).get("can_submit_counter_offer"):
            return {"ok": False, "error": {"code": "COUNTER_OFFER_NOT_AUTHORIZED"}}
        try:
            spec = OfferSpec.model_validate(args)
            offer = self._record(spec, "AGENT")
        except ValueError:
            return {"ok": False, "error": {"code": "NEGOTIATION_CONFLICT"}}
        self.commitments.append({"kind": "COUNTER", "offer": offer.copy(), "authorized": True})
        return {"ok": True, "offer": offer.copy()}

    def accept(self, offer_id):
        offer = self.offers.get(offer_id)
        if (
            offer_id != self.current_turn_offer_id
            or offer_id != self.current_offer_id
            or not offer
            or offer["status"] != "PROPOSED"
            or self.status != "ACTIVE"
            or not self.evaluate(**self._terms(offer))
            .get("authorization", {})
            .get("can_accept_automatically")
        ):
            return {"ok": False, "error": {"code": "OFFER_NOT_AUTHORIZED"}}
        offer["status"] = "ACCEPTED"
        self.version += 1
        self.commitments.append({"kind": "ACCEPT", "offer": offer.copy(), "authorized": True})
        return {"ok": True, "offer": offer.copy()}

    def request_approval(self, offer_id, reason):
        offer = self.offers.get(offer_id)
        if (
            offer_id != self.current_turn_offer_id
            or offer_id != self.current_offer_id
            or not offer
            or offer["status"] != "PROPOSED"
            or self.status != "ACTIVE"
            or not self.evaluate(**self._terms(offer))
            .get("authorization", {})
            .get("can_request_approval")
        ):
            return {"ok": False, "error": {"code": "APPROVAL_NOT_AUTHORIZED"}}
        self.approval = {
            "id": 1,
            "offer_id": offer_id,
            "status": "PENDING",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        }
        self.status = "WAITING_APPROVAL"
        self.version += 1
        return {"ok": True, "approval": self.approval.copy()}

    def process_approval(self):
        if not self.approval or self.approval["status"] != "PENDING":
            return None
        from app.agent.reply_policy import FormalReplyRenderer

        review = self.scenario.seller_review.value
        self.approval["status"] = review
        self.status = "ACTIVE"
        self.version += 1
        if review == "APPROVE":
            offer = self.offers[self.approval["offer_id"]]
            self.commitments.append({"kind": "APPROVAL", "offer": offer.copy(), "authorized": True})
            text = FormalReplyRenderer().render_approved_followup({"ok": True, "offer": offer})
        else:
            text = "卖家未同意这份报价，请调整交易条件后再提出。"
        self.approval_notifications.append(text)
        return text

    @staticmethod
    def _terms(offer):
        return {
            k: offer[k]
            for k in (
                "price",
                "shipping_paid_by",
                "shipping_cost",
                "seller_borne_discount",
                "additional_terms",
            )
        }

    def confirm(self, offer_id, goal):
        offer = self.offers.get(offer_id)
        approved = (
            self.approval
            and self.approval["status"] == "APPROVE"
            and self.approval["offer_id"] == offer_id
        )
        valid = (
            offer
            and offer_id == self.current_offer_id
            and (
                (offer["proposer"] == "AGENT" and offer["status"] == "PROPOSED")
                or offer["status"] == "ACCEPTED"
                or approved
            )
        )
        if not valid or not self.buyer_accepts(offer, goal):
            raise ValueError("Confirmation must reference a feasible valid offer")
        self.confirmed_offer_id = offer_id
        offer["status"] = "ACCEPTED"
        self.version += 1
        self.status = "AGREED"
        return offer

    @staticmethod
    def buyer_accepts(offer, goal):
        return Decimal(offer["price"]) <= goal.maximum_price and all(
            offer["additional_terms"].get(k) == v for k, v in goal.required_terms.items()
        )

    def tools(self):
        contracts = [
            ("get_product_info", "查询当前商品公开事实。", self.product, EmptyInput),
            ("get_negotiation_state", "查询协商状态与非敏感策略。", self.state, EmptyInput),
            (
                "evaluate_offer",
                "评估交易条件的权限，不返回私有阈值。",
                self.evaluate,
                OfferTermsInput,
            ),
            ("submit_counter_offer", "提交已授权的正式还价。", self.counter, OfferTermsInput),
            ("accept_offer", "接受本轮自动授权的买家正式报价。", self.accept, AcceptOfferInput),
            (
                "request_approval",
                "为本轮报价申请卖家审批。",
                self.request_approval,
                RequestApprovalInput,
            ),
        ]
        return [
            StructuredTool.from_function(
                func=func,
                name=name,
                description=description,
                args_schema=schema,
            )
            for name, description, func, schema in contracts
        ]
