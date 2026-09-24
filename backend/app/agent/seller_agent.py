from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from langchain_core.tools import BaseTool

from app.agent.decision import NegotiationAction, NegotiationDecision
from app.agent.decision_provider import DecisionProvider, DecisionRequest
from app.agent.reply_policy import (
    CandidateReplyGuard,
    FormalReplyRenderer,
    ReplySafetyError,
)


class AgentTurnOutcome(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    COUNTER_OFFERED = "COUNTER_OFFERED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    NEEDS_SELLER_CONFIRMATION = "NEEDS_SELLER_CONFIRMATION"
    REJECTED = "REJECTED"
    CLARIFICATION = "CLARIFICATION"
    SAFE_FAILURE = "SAFE_FAILURE"
    MODEL_ERROR = "MODEL_ERROR"


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    reply: str
    outcome: AgentTurnOutcome
    decision: NegotiationDecision | None
    formal_offer_id: int | None = None

    @property
    def is_formal_commitment(self) -> bool:
        return self.formal_offer_id is not None


class SellerAgent:
    """将模型候选决策、受约束工具和正式回复通路串联起来。"""

    _generic_failure_reply = "当前条件暂时无法安全处理，请换一种条件或稍后再试。"

    def __init__(
        self,
        *,
        decision_provider: DecisionProvider,
        tools: list[BaseTool],
        reply_guard: CandidateReplyGuard | None = None,
        reply_renderer: FormalReplyRenderer | None = None,
    ) -> None:
        self._decision_provider = decision_provider
        self._tools = {item.name: item for item in tools}
        required_tools = {
            "get_product_info",
            "get_negotiation_state",
            "evaluate_offer",
            "submit_counter_offer",
            "accept_offer",
        }
        if len(tools) != len(required_tools) or set(self._tools) != required_tools:
            raise ValueError("SellerAgent requires exactly the five V1 negotiation tools")
        self._reply_guard = reply_guard or CandidateReplyGuard()
        self._reply_renderer = reply_renderer or FormalReplyRenderer()

    def handle_turn(self, buyer_message: str) -> AgentTurnResult:
        normalized_message = buyer_message.strip()
        if not normalized_message or len(normalized_message) > 4000:
            return AgentTurnResult(
                reply="请输入有效且不过长的咨询内容。",
                outcome=AgentTurnOutcome.SAFE_FAILURE,
                decision=None,
            )

        product_result = self._invoke("get_product_info", {})
        negotiation_result = self._invoke("get_negotiation_state", {})
        if not self._is_success(product_result) or not self._is_success(negotiation_result):
            return AgentTurnResult(
                reply=self._generic_failure_reply,
                outcome=AgentTurnOutcome.SAFE_FAILURE,
                decision=None,
            )

        try:
            decision = self._decision_provider.decide(
                DecisionRequest(
                    buyer_message=normalized_message,
                    product_context=product_result,
                    negotiation_context=negotiation_result,
                )
            )
        except Exception:
            # 模型或结构化解析错误不得向买家泄漏内部异常，也不得触发正式承诺。
            return AgentTurnResult(
                reply="暂时无法可靠理解这条消息，请换一种说法后重试。",
                outcome=AgentTurnOutcome.MODEL_ERROR,
                decision=None,
            )

        return self._execute_decision(
            decision=decision,
            product_result=product_result,
            negotiation_result=negotiation_result,
        )

    def _execute_decision(
        self,
        *,
        decision: NegotiationDecision,
        product_result: dict[str, object],
        negotiation_result: dict[str, object],
    ) -> AgentTurnResult:
        if decision.action is NegotiationAction.COUNTER:
            return self._execute_counter(decision)
        if decision.action is NegotiationAction.ACCEPT:
            return self._execute_accept(decision)
        if decision.action is NegotiationAction.REQUEST_APPROVAL:
            return self._execute_approval_hint(decision, negotiation_result)

        product = product_result.get("product")
        if not isinstance(product, dict):
            return self._safe_failure(decision)
        try:
            listed_price = self._decimal_value(product["listed_price"])
        except (KeyError, ValueError):
            return self._safe_failure(decision)

        fallbacks = {
            NegotiationAction.INQUIRY: (
                "我可以根据商品页面中的公开信息回答；涉及价格或交易条件时，"
                "需要先经过系统确认。"
            ),
            NegotiationAction.REJECT: "这个条件暂时无法接受，你可以调整后再提出。",
            NegotiationAction.CLARIFY: "请补充你希望确认的具体商品或交易条件。",
        }
        outcomes = {
            NegotiationAction.INQUIRY: AgentTurnOutcome.INFORMATIONAL,
            NegotiationAction.REJECT: AgentTurnOutcome.REJECTED,
            NegotiationAction.CLARIFY: AgentTurnOutcome.CLARIFICATION,
        }
        fallback = fallbacks[decision.action]
        reply = self._reply_guard.safe_informational_reply(
            candidate=decision.reply,
            listed_price=listed_price,
            fallback=fallback,
        )
        return AgentTurnResult(
            reply=reply,
            outcome=outcomes[decision.action],
            decision=decision,
        )

    def _execute_counter(self, decision: NegotiationDecision) -> AgentTurnResult:
        payload: dict[str, object] = {
            "price": decision.proposed_price,
            "shipping_paid_by": decision.shipping_paid_by,
            "additional_terms": decision.additional_terms,
        }
        if decision.shipping_cost is not None:
            payload["shipping_cost"] = decision.shipping_cost
        if decision.seller_borne_discount is not None:
            payload["seller_borne_discount"] = decision.seller_borne_discount

        tool_result = self._invoke("submit_counter_offer", payload)
        if not self._is_success(tool_result):
            return self._safe_failure(decision)
        try:
            reply = self._reply_renderer.render_counter_offer(tool_result)
            offer_id = self._offer_id(tool_result)
        except ReplySafetyError:
            return self._safe_failure(decision)
        return AgentTurnResult(
            reply=reply,
            outcome=AgentTurnOutcome.COUNTER_OFFERED,
            decision=decision,
            formal_offer_id=offer_id,
        )

    def _execute_accept(self, decision: NegotiationDecision) -> AgentTurnResult:
        tool_result = self._invoke("accept_offer", {"offer_id": decision.offer_id})
        if not self._is_success(tool_result):
            return self._safe_failure(decision)
        try:
            reply = self._reply_renderer.render_accepted_offer(tool_result)
            offer_id = self._offer_id(tool_result)
        except ReplySafetyError:
            return self._safe_failure(decision)
        return AgentTurnResult(
            reply=reply,
            outcome=AgentTurnOutcome.OFFER_ACCEPTED,
            decision=decision,
            formal_offer_id=offer_id,
        )

    def _execute_approval_hint(
        self,
        decision: NegotiationDecision,
        negotiation_result: dict[str, object],
    ) -> AgentTurnResult:
        offer = self._current_offer(negotiation_result, decision.offer_id)
        if offer is None or offer.get("proposer") != "BUYER":
            return self._safe_failure(decision)
        evaluation = self._invoke(
            "evaluate_offer",
            {
                "price": offer.get("price"),
                "shipping_paid_by": offer.get("shipping_paid_by"),
                "shipping_cost": offer.get("shipping_cost"),
                "seller_borne_discount": offer.get("seller_borne_discount"),
                "additional_terms": offer.get("additional_terms") or {},
            },
        )
        authorization = evaluation.get("authorization")
        if (
            evaluation.get("ok") is True
            and isinstance(authorization, dict)
            and authorization.get("conditions_valid") is True
            and authorization.get("can_request_approval") is True
        ):
            return AgentTurnResult(
                reply="这个报价需要卖家确认，目前还不能直接答应，请稍等后续结果。",
                outcome=AgentTurnOutcome.NEEDS_SELLER_CONFIRMATION,
                decision=decision,
            )
        return AgentTurnResult(
            reply="这个条件目前无法直接接受，你可以调整报价或交易条件。",
            outcome=AgentTurnOutcome.REJECTED,
            decision=decision,
        )

    def _invoke(self, tool_name: str, payload: dict[str, object]) -> dict[str, object]:
        result = self._tools[tool_name].invoke(payload)
        if isinstance(result, dict):
            return result
        return {"ok": False}

    @staticmethod
    def _is_success(result: dict[str, object]) -> bool:
        return result.get("ok") is True

    def _safe_failure(self, decision: NegotiationDecision) -> AgentTurnResult:
        return AgentTurnResult(
            reply=self._generic_failure_reply,
            outcome=AgentTurnOutcome.SAFE_FAILURE,
            decision=decision,
        )

    @staticmethod
    def _offer_id(tool_result: dict[str, object]) -> int:
        offer = tool_result.get("offer")
        if (
            not isinstance(offer, dict)
            or type(offer.get("id")) is not int
            or offer["id"] <= 0
        ):
            raise ReplySafetyError("正式报价缺少有效编号")
        return offer["id"]

    @staticmethod
    def _current_offer(
        negotiation_result: dict[str, object],
        offer_id: int | None,
    ) -> dict[str, object] | None:
        negotiation = negotiation_result.get("negotiation")
        if not isinstance(negotiation, dict):
            return None
        if negotiation.get("current_offer_id") != offer_id:
            return None
        offers = negotiation.get("recent_offers")
        if not isinstance(offers, list):
            return None
        return next(
            (
                offer
                for offer in offers
                if isinstance(offer, dict) and offer.get("id") == offer_id
            ),
            None,
        )

    @staticmethod
    def _decimal_value(value: object) -> Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("invalid decimal") from exc
