from dataclasses import dataclass
from enum import StrEnum

from langchain_core.tools import BaseTool

from app.agent.decision import NegotiationAction, NegotiationDecision
from app.agent.decision_provider import (
    ConversationMessage,
    DecisionProvider,
    DecisionRequest,
)
from app.agent.reply_policy import (
    DialoguePolicyService,
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


@dataclass(frozen=True, slots=True)
class AgentTurnPreparation:
    """模型调用前冻结的可信上下文或无需模型的确定性结果。"""

    decision_request: DecisionRequest | None = None
    deterministic_decision: NegotiationDecision | None = None
    immediate_result: AgentTurnResult | None = None

    @property
    def requires_model(self) -> bool:
        return (
            self.immediate_result is None
            and self.deterministic_decision is None
            and self.decision_request is not None
        )


class SellerAgent:
    """将模型候选决策、受约束工具和正式回复通路串联起来。"""

    _generic_failure_reply = "当前条件暂时无法安全处理，请换一种条件或稍后再试。"

    def __init__(
        self,
        *,
        decision_provider: DecisionProvider,
        tools: list[BaseTool],
        reply_renderer: FormalReplyRenderer | None = None,
        dialogue_policy: DialoguePolicyService | None = None,
    ) -> None:
        self._decision_provider = decision_provider
        self._tools = {item.name: item for item in tools}
        required_tools = {
            "get_product_info",
            "get_negotiation_state",
            "evaluate_offer",
            "submit_counter_offer",
            "accept_offer",
            "request_approval",
        }
        if len(tools) != len(required_tools) or set(self._tools) != required_tools:
            raise ValueError("SellerAgent requires exactly the six V2 negotiation tools")
        self._reply_renderer = reply_renderer or FormalReplyRenderer()
        self._dialogue_policy = dialogue_policy or DialoguePolicyService()

    def handle_turn(
        self,
        buyer_message: str,
        *,
        conversation_history: tuple[ConversationMessage, ...] = (),
        current_turn_offer_id: int | None = None,
    ) -> AgentTurnResult:
        preparation = self.prepare_turn(
            buyer_message,
            conversation_history=conversation_history,
            current_turn_offer_id=current_turn_offer_id,
        )
        if not preparation.requires_model:
            return self.apply_prepared(preparation)

        try:
            if preparation.decision_request is None:  # pragma: no cover - 防御分支
                raise RuntimeError("模型决策缺少输入快照")
            decision = self._decision_provider.decide(preparation.decision_request)
        except Exception:
            # 模型或结构化解析错误不得向买家泄漏内部异常，也不得触发正式承诺。
            return AgentTurnResult(
                reply="暂时无法可靠理解这条消息，请换一种说法后重试。",
                outcome=AgentTurnOutcome.MODEL_ERROR,
                decision=None,
            )
        return self.apply_prepared(preparation, decision=decision)

    def prepare_turn(
        self,
        buyer_message: str,
        *,
        conversation_history: tuple[ConversationMessage, ...] = (),
        current_turn_offer_id: int | None = None,
    ) -> AgentTurnPreparation:
        """只读取可信上下文和规则授权，不调用模型或执行变更工具。"""

        normalized_message = buyer_message.strip()
        if not normalized_message or len(normalized_message) > 4000:
            return AgentTurnPreparation(
                immediate_result=AgentTurnResult(
                    reply="请输入有效且不过长的咨询内容。",
                    outcome=AgentTurnOutcome.SAFE_FAILURE,
                    decision=None,
                )
            )

        product_result = self._invoke("get_product_info", {})
        negotiation_result = self._invoke("get_negotiation_state", {})
        if not self._is_success(product_result) or not self._is_success(negotiation_result):
            return AgentTurnPreparation(
                immediate_result=AgentTurnResult(
                    reply=self._generic_failure_reply,
                    outcome=AgentTurnOutcome.SAFE_FAILURE,
                    decision=None,
                )
            )

        decision_negotiation_context = self._with_current_offer_authorization(
            negotiation_result,
            current_turn_offer_id=current_turn_offer_id,
        )
        if decision_negotiation_context is None:
            return AgentTurnPreparation(
                immediate_result=AgentTurnResult(
                    reply=self._generic_failure_reply,
                    outcome=AgentTurnOutcome.SAFE_FAILURE,
                    decision=None,
                )
            )

        decision_request = DecisionRequest(
            buyer_message=normalized_message,
            product_context=product_result,
            negotiation_context=decision_negotiation_context,
            conversation_history=conversation_history,
            current_turn_offer_id=current_turn_offer_id,
        )
        deterministic_result = self._authorized_current_offer_decision(
            decision_negotiation_context,
            current_turn_offer_id=current_turn_offer_id,
        )
        if isinstance(deterministic_result, AgentTurnResult):
            return AgentTurnPreparation(immediate_result=deterministic_result)
        return AgentTurnPreparation(
            decision_request=decision_request,
            deterministic_decision=deterministic_result,
        )

    def apply_prepared(
        self,
        preparation: AgentTurnPreparation,
        *,
        decision: NegotiationDecision | None = None,
    ) -> AgentTurnResult:
        """把已冻结的决策交给受约束工具；调用方负责先复核数据库版本。"""

        if preparation.immediate_result is not None:
            return preparation.immediate_result
        request = preparation.decision_request
        if request is None:
            raise ValueError("AgentTurnPreparation 缺少决策上下文")
        resolved_decision = preparation.deterministic_decision
        if resolved_decision is None:
            if decision is None:
                raise ValueError("模型决策尚未提供")
            resolved_decision = self._constrain_current_offer_decision(
                decision,
                current_turn_offer_id=request.current_turn_offer_id,
            )
        return self._execute_decision(
            decision=resolved_decision,
            product_result=request.product_context,
            negotiation_result=request.negotiation_context,
            current_turn_offer_id=request.current_turn_offer_id,
        )

    def _authorized_current_offer_decision(
        self,
        negotiation_result: dict[str, object],
        *,
        current_turn_offer_id: int | None,
    ) -> NegotiationDecision | AgentTurnResult | None:
        """为完整正式报价生成后端已能确定的接受或审批决策。"""

        if current_turn_offer_id is None:
            return None
        authorization = negotiation_result.get("current_offer_authorization")
        if not isinstance(authorization, dict):
            return AgentTurnResult(
                reply=self._generic_failure_reply,
                outcome=AgentTurnOutcome.SAFE_FAILURE,
                decision=None,
            )

        if authorization.get("can_accept_automatically") is True:
            return NegotiationDecision(
                action=NegotiationAction.ACCEPT,
                offer_id=current_turn_offer_id,
                reason="后端规则授权自动接受本轮正式报价",
                reply="由正式回复安全层生成接受结果。",
            )

        if authorization.get("can_request_approval") is True:
            return NegotiationDecision(
                action=NegotiationAction.REQUEST_APPROVAL,
                offer_id=current_turn_offer_id,
                reason="后端规则要求卖家确认本轮正式报价",
                reply="由正式回复安全层生成审批结果。",
            )

        return None

    @staticmethod
    def _constrain_current_offer_decision(
        decision: NegotiationDecision,
        *,
        current_turn_offer_id: int | None,
    ) -> NegotiationDecision:
        """禁止完整正式报价被模型降级为咨询、澄清或越权动作。"""

        if current_turn_offer_id is None or decision.action in {
            NegotiationAction.COUNTER,
            NegotiationAction.REJECT,
        }:
            return decision
        return NegotiationDecision(
            action=NegotiationAction.REJECT,
            reason="本轮正式报价未获得自动接受或卖家审批授权",
            reply="由正式回复安全层生成拒绝结果。",
        )

    def _execute_decision(
        self,
        *,
        decision: NegotiationDecision,
        product_result: dict[str, object],
        negotiation_result: dict[str, object],
        current_turn_offer_id: int | None,
    ) -> AgentTurnResult:
        if decision.action is NegotiationAction.COUNTER:
            return self._execute_counter(decision)
        if decision.action is NegotiationAction.ACCEPT:
            return self._execute_accept(decision, current_turn_offer_id)
        if decision.action is NegotiationAction.REQUEST_APPROVAL:
            return self._execute_approval(
                decision,
                current_turn_offer_id,
            )

        product = product_result.get("product")
        if not isinstance(product, dict):
            return self._safe_failure(decision)

        if decision.action is NegotiationAction.INQUIRY:
            if not decision.dialogue_acts:
                return self._safe_failure(decision)
            dialogue_reply = self._dialogue_policy.resolve(
                acts=decision.dialogue_acts,
                candidate_reply=decision.reply,
                product=product,
            )
            outcome = (
                AgentTurnOutcome.CLARIFICATION
                if dialogue_reply.needs_clarification
                else AgentTurnOutcome.INFORMATIONAL
            )
            reply = dialogue_reply.text
        else:
            outcome = AgentTurnOutcome.REJECTED
            reply = "这个条件暂时无法接受，你可以调整后再提出。"
        return AgentTurnResult(
            # 低风险咨询可使用通过校验的候选文案；业务条件由回复计划生成。
            reply=reply,
            outcome=outcome,
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

    def _execute_accept(
        self,
        decision: NegotiationDecision,
        current_turn_offer_id: int | None,
    ) -> AgentTurnResult:
        if decision.offer_id != current_turn_offer_id:
            return self._safe_failure(decision)
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

    def _execute_approval(
        self,
        decision: NegotiationDecision,
        current_turn_offer_id: int | None,
    ) -> AgentTurnResult:
        if decision.offer_id != current_turn_offer_id:
            return self._safe_failure(decision)
        tool_result = self._invoke(
            "request_approval",
            {"offer_id": decision.offer_id, "reason": decision.reason},
        )
        if not self._is_success(tool_result):
            if self._error_code(tool_result) == "APPROVAL_NOT_AUTHORIZED":
                return AgentTurnResult(
                    reply="这个条件目前无法接受，你可以调整报价或交易条件。",
                    outcome=AgentTurnOutcome.REJECTED,
                    decision=decision,
                )
            return self._safe_failure(decision)
        if not self._is_valid_approval(tool_result, offer_id=decision.offer_id):
            return self._safe_failure(decision)
        return AgentTurnResult(
            reply="已将这份报价提交卖家确认，目前还不能视为接受或成交，请等待后续结果。",
            outcome=AgentTurnOutcome.NEEDS_SELLER_CONFIRMATION,
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

    @staticmethod
    def _error_code(result: dict[str, object]) -> str | None:
        error = result.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        return code if isinstance(code, str) else None

    @staticmethod
    def _is_valid_approval(
        tool_result: dict[str, object],
        *,
        offer_id: int | None,
    ) -> bool:
        approval = tool_result.get("approval")
        return (
            isinstance(approval, dict)
            and type(approval.get("id")) is int
            and approval["id"] > 0
            and approval.get("offer_id") == offer_id
            and approval.get("status") == "PENDING"
        )

    def _safe_failure(self, decision: NegotiationDecision) -> AgentTurnResult:
        return AgentTurnResult(
            reply=self._generic_failure_reply,
            outcome=AgentTurnOutcome.SAFE_FAILURE,
            decision=decision,
        )

    def _with_current_offer_authorization(
        self,
        negotiation_result: dict[str, object],
        *,
        current_turn_offer_id: int | None,
    ) -> dict[str, object] | None:
        """向模型提供非敏感执行权限，不暴露卖家的具体价格阈值。"""

        if current_turn_offer_id is None:
            return negotiation_result
        offer = self._current_offer(negotiation_result, current_turn_offer_id)
        if offer is None or offer.get("proposer") != "BUYER":
            return None
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
        if evaluation.get("ok") is not True or not isinstance(authorization, dict):
            return None
        return {
            **negotiation_result,
            "current_offer_authorization": dict(authorization),
        }

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
