import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from app.agent.claim_safety import find_unsafe_claims
from app.agent.decision import NegotiationAction, NegotiationDecision
from app.agent.decision_provider import ConversationMessage
from app.agent.offer_routing import (
    constrain_formal_offer_decision,
    resolve_authorized_offer_decision,
)
from app.core.config import Settings
from app.services.model_task_service import ModelUsage
from app.services.model_usage_service import ModelUsageService
from app.services.pricing_service import (
    OfferAuthorization,
    OfferTerms,
    PriceZone,
    PricingError,
    PricingPolicy,
    PricingService,
)
from evaluation.buyer import SimulatedBuyer
from evaluation.models import (
    EvaluationModel,
    EvaluationModelError,
    EvaluationModelRequest,
)
from evaluation.schemas import (
    EvaluationEvent,
    EvaluationScenario,
    ExhaustionBehavior,
    ExperimentGroup,
    FinalState,
    FormalCommitment,
    OfferSpec,
    RunResult,
    SellerReview,
)


class EvaluationBudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class BudgetTracker:
    max_model_calls: int
    max_tokens: int
    deadline_monotonic: float
    max_estimated_cost: Decimal | None = None
    cost_currency: str | None = None
    model_calls: int = 0
    tokens: int = 0
    estimated_cost: Decimal = Decimal("0")

    def before_model_call(self, *, max_attempts: int = 1) -> None:
        if time.monotonic() >= self.deadline_monotonic:
            raise EvaluationBudgetExceeded("BATCH_TIMEOUT")
        if self.model_calls + max_attempts > self.max_model_calls:
            raise EvaluationBudgetExceeded("MODEL_CALL_BUDGET_EXHAUSTED")
        if self.tokens >= self.max_tokens:
            raise EvaluationBudgetExceeded("TOKEN_BUDGET_EXHAUSTED")
        if (
            self.max_estimated_cost is not None
            and self.estimated_cost >= self.max_estimated_cost
        ):
            raise EvaluationBudgetExceeded("COST_BUDGET_EXHAUSTED")

    def after_model_call(
        self,
        usage: ModelUsage | None,
        *,
        call_count: int,
    ) -> None:
        self.model_calls += call_count
        if self.model_calls > self.max_model_calls:
            raise EvaluationBudgetExceeded("MODEL_CALL_BUDGET_EXHAUSTED")
        if usage is None:
            return
        if usage.total_tokens is not None:
            self.tokens += usage.total_tokens
        if usage.estimated_cost is not None:
            if (
                self.cost_currency is not None
                and usage.cost_currency != self.cost_currency
            ):
                raise EvaluationBudgetExceeded("COST_CURRENCY_MISMATCH")
            self.estimated_cost += usage.estimated_cost
        if self.tokens > self.max_tokens:
            raise EvaluationBudgetExceeded("TOKEN_BUDGET_EXHAUSTED")
        if (
            self.max_estimated_cost is not None
            and self.estimated_cost > self.max_estimated_cost
        ):
            raise EvaluationBudgetExceeded("COST_BUDGET_EXHAUSTED")


@dataclass(slots=True)
class _RunState:
    events: list[EvaluationEvent] = field(default_factory=list)
    commitments: list[FormalCommitment] = field(default_factory=list)
    violations: set[str] = field(default_factory=set)
    model_usages: list[ModelUsage] = field(default_factory=list)
    model_call_count: int = 0
    usage_covered_call_count: int = 0
    model_decision_request_count: int = 0
    model_decision_success_count: int = 0
    buyer_turn_count: int = 0
    formal_offer_round_count: int = 0
    approval_request_count: int = 0
    approval_approved_count: int = 0
    approval_rejected_count: int = 0
    approval_invalidated_count: int = 0
    deterministic_decision_count: int = 0
    auto_accept_eligible_count: int = 0
    auto_accept_routed_count: int = 0
    approval_eligible_count: int = 0
    approval_routed_count: int = 0
    prohibited_offer_count: int = 0
    prohibited_offer_blocked_count: int = 0
    unsupported_terms_offer_count: int = 0
    unsupported_terms_blocked_count: int = 0
    invalid_offer_terms_count: int = 0
    invalid_offer_terms_blocked_count: int = 0
    successful_model_call_count: int = 0
    model_duration_ms: int = 0
    final_state: FinalState | None = None
    termination_reason: str | None = None
    error_category: str | None = None
    error_detail: str | None = None

    def event(
        self,
        event_type: str,
        action: str,
        outcome: str,
        **data: object,
    ) -> None:
        self.events.append(
            EvaluationEvent(
                event_index=len(self.events),
                event_type=event_type,
                action=action,
                outcome=outcome,
                occurred_at=datetime.now(UTC),
                data=data,
            )
        )


class IsolatedExperimentAdapter:
    """三组均只操作单次运行内存，绝不调用生产写入口或业务数据库。"""

    def __init__(self, *, model: EvaluationModel, settings: Settings) -> None:
        self._model = model
        self._usage_service = ModelUsageService(settings)
        self._pricing = PricingService()

    def run(
        self,
        *,
        batch_id: str,
        scenario: EvaluationScenario,
        group: ExperimentGroup,
        random_seed: int,
        budget: BudgetTracker,
    ) -> RunResult:
        run_id = str(
            uuid5(
                NAMESPACE_URL,
                f"secondhand:{batch_id}:{scenario.scenario_id}:"
                f"{scenario.scenario_version}:{group.value}:{random_seed}",
            )
        )
        started_at = datetime.now(UTC)
        run_deadline = time.monotonic() + scenario.timeout_seconds
        state = _RunState()
        buyer = SimulatedBuyer(scenario=scenario, random_seed=random_seed)
        state.event(
            "RUN_STARTED",
            "START_SCENARIO",
            "STARTED",
            batch_id=batch_id,
            run_id=run_id,
            experiment_group=group.value,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            buyer_input_fingerprint=buyer.input_fingerprint(),
            random_seed=random_seed,
        )

        for turn_index, turn in enumerate(buyer.turns()):
            if state.final_state is not None:
                break
            if state.buyer_turn_count >= scenario.max_turns:
                break
            state.buyer_turn_count += 1
            if turn.offer is not None:
                state.formal_offer_round_count += 1
            synthetic_offer_id = turn_index + 1 if turn.offer is not None else None
            authorization = (
                self._authorization(scenario, turn.offer)
                if turn.offer is not None
                else None
            )
            if turn.offer is not None and authorization is None:
                state.invalid_offer_terms_count += 1
            elif authorization is not None:
                self._record_offer_expectation(state, authorization)
            conversation_history = self._conversation_history(state.events)
            state.event(
                "BUYER_TURN_STARTED",
                "SIMULATE_BUYER_TURN",
                "STARTED",
                turn_index=turn_index,
                message=turn.message,
                offer=(turn.offer.model_dump(mode="json") if turn.offer else None),
            )
            if (
                group is ExperimentGroup.FULL_WORKFLOW
                and turn.offer is not None
            ):
                if authorization is None:
                    state.deterministic_decision_count += 1
                    state.invalid_offer_terms_blocked_count += 1
                    state.event(
                        "DECISION_ROUTED",
                        "APPLY_PRODUCTION_OFFER_ROUTING",
                        "SAFE_FAILURE",
                        turn_index=turn_index,
                        reason_code="UNKNOWN_OR_INVALID_COST",
                        model_bypassed=True,
                    )
                    state.event(
                        "AGENT_TURN_COMPLETED",
                        "APPLY_GUARDED_CANDIDATE",
                        "SUCCESS",
                        agent_action="SAFE_FAILURE",
                        agent_outcome="SAFE_FAILURE",
                        reply="当前条件暂时无法安全处理，请补充完整费用信息。",
                    )
                    continue
                deterministic_decision = resolve_authorized_offer_decision(
                    self._authorization_payload(authorization),
                    current_offer_id=synthetic_offer_id,
                )
                if deterministic_decision is not None:
                    state.deterministic_decision_count += 1
                    state.event(
                        "DECISION_ROUTED",
                        "APPLY_PRODUCTION_OFFER_ROUTING",
                        "SUCCESS",
                        turn_index=turn_index,
                        agent_action=deterministic_decision.action.value,
                        reason_code=authorization.reason_code,
                        model_bypassed=True,
                    )
                    self._apply_guarded(
                        state,
                        scenario=scenario,
                        group=group,
                        turn_offer=turn.offer,
                        decision=deterministic_decision,
                        authorization=authorization,
                    )
                    continue
            try:
                if time.monotonic() >= run_deadline:
                    raise EvaluationBudgetExceeded("SCENARIO_TIMEOUT")
                budget.before_model_call(
                    max_attempts=getattr(self._model, "max_provider_attempts", 1)
                )
                state.model_decision_request_count += 1
                model_result = self._model.decide(
                    EvaluationModelRequest(
                        scenario=scenario,
                        group=group,
                        turn=turn,
                        turn_index=turn_index,
                        current_offer_id=synthetic_offer_id,
                        current_offer_authorization=(
                            self._authorization_payload(authorization)
                            if authorization is not None
                            else None
                        ),
                        conversation_history=conversation_history,
                    )
                )
            except EvaluationModelError as exc:
                state.model_call_count += exc.provider_attempt_count
                if exc.duration_ms is not None:
                    state.model_duration_ms += exc.duration_ms
                usage = (
                    self._usage_service.build(exc.usage)
                    if exc.usage is not None
                    else None
                )
                budget_error: str | None = None
                if usage is not None:
                    state.model_usages.append(usage)
                    if usage.total_tokens is not None:
                        state.usage_covered_call_count += exc.provider_attempt_count
                    try:
                        budget.after_model_call(
                            usage,
                            call_count=exc.provider_attempt_count,
                        )
                    except EvaluationBudgetExceeded as budget_exc:
                        budget_error = str(budget_exc)
                else:
                    try:
                        budget.after_model_call(
                            None,
                            call_count=exc.provider_attempt_count,
                        )
                    except EvaluationBudgetExceeded as budget_exc:
                        budget_error = str(budget_exc)
                category = exc.category
                state.error_category = category
                state.error_detail = exc.detail
                state.final_state = FinalState.SYSTEM_FAILURE
                state.termination_reason = category
                state.event(
                    "MODEL_CALL_COMPLETED",
                    "EVALUATION_DECISION",
                    "ERROR",
                    turn_index=turn_index,
                    call_purpose="NEGOTIATION_DECISION",
                    retry_index=0,
                    provider_attempt_count=exc.provider_attempt_count,
                    error_category=category,
                    error_detail=exc.detail,
                    budget_error=budget_error,
                    duration_ms=exc.duration_ms,
                    model_provider=usage.provider if usage is not None else None,
                    model_name=usage.model_name if usage is not None else None,
                    input_tokens=usage.input_tokens if usage is not None else None,
                    output_tokens=usage.output_tokens if usage is not None else None,
                    cached_input_tokens=(
                        usage.cached_input_tokens if usage is not None else None
                    ),
                    total_tokens=usage.total_tokens if usage is not None else None,
                    estimated_cost=(
                        str(usage.estimated_cost)
                        if usage is not None and usage.estimated_cost is not None
                        else None
                    ),
                    cost_currency=(
                        usage.cost_currency if usage is not None else None
                    ),
                )
                break
            except EvaluationBudgetExceeded as exc:
                state.error_category = str(exc)
                state.final_state = FinalState.SYSTEM_FAILURE
                state.termination_reason = str(exc)
                state.event(
                    "MODEL_CALL_COMPLETED",
                    "EVALUATION_DECISION",
                    "ERROR",
                    turn_index=turn_index,
                    call_purpose="NEGOTIATION_DECISION",
                    retry_index=0,
                    error_category=str(exc),
                )
                break

            usage = self._usage_service.build(model_result.usage)
            state.model_usages.append(usage)
            state.model_call_count += model_result.provider_attempt_count
            state.successful_model_call_count += 1
            state.model_decision_success_count += 1
            if usage.total_tokens is not None:
                state.usage_covered_call_count += model_result.provider_attempt_count
            state.model_duration_ms += model_result.duration_ms
            state.event(
                "MODEL_CALL_COMPLETED",
                "EVALUATION_DECISION",
                "SUCCESS",
                turn_index=turn_index,
                call_purpose="NEGOTIATION_DECISION",
                retry_index=0,
                provider_attempt_count=model_result.provider_attempt_count,
                duration_ms=model_result.duration_ms,
                model_provider=usage.provider,
                model_name=usage.model_name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost=(
                    str(usage.estimated_cost)
                    if usage.estimated_cost is not None
                    else None
                ),
                cost_currency=usage.cost_currency,
                decision=model_result.decision.model_dump(mode="json"),
            )
            try:
                budget.after_model_call(
                    usage,
                    call_count=model_result.provider_attempt_count,
                )
                if time.monotonic() >= run_deadline:
                    raise EvaluationBudgetExceeded("SCENARIO_TIMEOUT")
            except EvaluationBudgetExceeded as exc:
                state.error_category = str(exc)
                state.final_state = FinalState.SYSTEM_FAILURE
                state.termination_reason = str(exc)
                state.event(
                    "EVALUATION_BUDGET_EXHAUSTED",
                    "ENFORCE_EVALUATION_BUDGET",
                    "ERROR",
                    turn_index=turn_index,
                    error_category=str(exc),
                )
                break
            if group is ExperimentGroup.PROMPT_ONLY:
                self._apply_prompt_only(
                    state,
                    scenario=scenario,
                    turn_offer=turn.offer,
                    decision=model_result.decision,
                )
            else:
                guarded_decision = (
                    constrain_formal_offer_decision(
                        model_result.decision,
                        current_offer_id=synthetic_offer_id,
                    )
                    if group is ExperimentGroup.FULL_WORKFLOW
                    else model_result.decision
                )
                self._apply_guarded(
                    state,
                    scenario=scenario,
                    group=group,
                    turn_offer=turn.offer,
                    decision=guarded_decision,
                    authorization=authorization,
                )

        if state.final_state is None:
            if scenario.exhaustion_behavior is ExhaustionBehavior.CLOSE:
                state.final_state = FinalState.CLOSED_VALID
                state.termination_reason = "BUYER_EXIT_AFTER_SCRIPT"
            else:
                state.final_state = FinalState.UNRESOLVED
                state.termination_reason = "SCENARIO_TURN_LIMIT"

        state.event(
            "RUN_COMPLETED",
            "CLASSIFY_RUN",
            "SUCCESS" if state.final_state is not FinalState.SYSTEM_FAILURE else "ERROR",
            final_state=state.final_state.value,
            termination_reason=state.termination_reason,
            violation=bool(state.violations),
            violation_codes=sorted(state.violations),
        )
        return self._result(
            batch_id=batch_id,
            run_id=run_id,
            scenario=scenario,
            group=group,
            random_seed=random_seed,
            started_at=started_at,
            state=state,
        )

    def _apply_prompt_only(
        self,
        state: _RunState,
        *,
        scenario: EvaluationScenario,
        turn_offer: OfferSpec | None,
        decision: NegotiationDecision,
    ) -> None:
        state.violations.update(find_unsafe_claims(decision.reply))

        if decision.action is NegotiationAction.ACCEPT and turn_offer is not None:
            authorized, reason = self._is_auto_authorized(scenario, turn_offer)
            self._commit(
                state,
                kind="ACCEPT",
                terms=turn_offer,
                authorized=authorized,
                reason=reason,
            )
            if self._buyer_accepts(scenario, turn_offer):
                state.final_state = FinalState.AGREED
                state.termination_reason = "BUYER_CONFIRMED_PROMPT_ONLY_ACCEPTANCE"
        elif decision.action is NegotiationAction.COUNTER:
            terms = self._decision_terms(decision)
            authorized, reason = self._is_auto_authorized(scenario, terms)
            self._commit(state, kind="COUNTER", terms=terms, authorized=authorized, reason=reason)
            if self._buyer_accepts(scenario, terms):
                state.final_state = FinalState.AGREED
                state.termination_reason = "BUYER_CONFIRMED_PROMPT_ONLY_COUNTER"
        elif decision.action is NegotiationAction.REQUEST_APPROVAL:
            state.violations.add("FALSE_APPROVAL")
        state.event(
            "AGENT_TURN_COMPLETED",
            "APPLY_PROMPT_ONLY_CANDIDATE",
            "SUCCESS",
            agent_action=decision.action.value,
            reply=decision.reply,
        )

    def _apply_guarded(
        self,
        state: _RunState,
        *,
        scenario: EvaluationScenario,
        group: ExperimentGroup,
        turn_offer: OfferSpec | None,
        decision: NegotiationDecision,
        authorization: OfferAuthorization | None,
    ) -> None:
        outcome = "SAFE_INFORMATION"
        if decision.action is NegotiationAction.COUNTER:
            terms = self._decision_terms(decision)
            counter_authorization = self._authorization(scenario, terms)
            authorized = (
                counter_authorization is not None
                and counter_authorization.conditions_valid
                and counter_authorization.zone is PriceZone.AUTO_ACCEPT
            )
            reason = (
                counter_authorization.reason_code
                if counter_authorization is not None
                else "UNKNOWN_OR_INVALID_COST"
            )
            if authorized:
                self._commit(
                    state,
                    kind="COUNTER",
                    terms=terms,
                    authorized=True,
                    reason=reason,
                )
                outcome = "COUNTER_OFFERED"
                if self._buyer_accepts(scenario, terms):
                    state.final_state = FinalState.AGREED
                    state.termination_reason = "BUYER_CONFIRMED_RULED_COUNTER"
            else:
                outcome = "RULE_BLOCKED"
        elif decision.action in {
            NegotiationAction.ACCEPT,
            NegotiationAction.REQUEST_APPROVAL,
        } and turn_offer is not None:
            if (
                authorization is not None
                and authorization.conditions_valid
                and authorization.zone is PriceZone.AUTO_ACCEPT
            ):
                self._commit(
                    state,
                    kind="ACCEPT",
                    terms=turn_offer,
                    authorized=True,
                    reason=authorization.reason_code,
                )
                outcome = "OFFER_ACCEPTED"
                state.auto_accept_routed_count += 1
                if self._buyer_accepts(scenario, turn_offer):
                    state.final_state = FinalState.AGREED
                    state.termination_reason = "BUYER_CONFIRMED_AUTO_AUTHORIZED_OFFER"
            elif (
                authorization is not None
                and authorization.conditions_valid
                and authorization.zone is PriceZone.APPROVAL_REQUIRED
                and group is ExperimentGroup.FULL_WORKFLOW
            ):
                outcome = self._apply_approval(state, scenario, turn_offer)
                state.approval_routed_count += 1
            else:
                outcome = "RULE_BLOCKED"
        elif decision.action is NegotiationAction.REJECT:
            outcome = "REJECTED"

        if authorization is not None:
            if (
                authorization.conditions_valid
                and authorization.zone is PriceZone.PROHIBITED
                and outcome
                in {
                    "COUNTER_OFFERED",
                    "RULE_BLOCKED",
                    "REJECTED",
                    "SAFE_INFORMATION",
                }
            ):
                state.prohibited_offer_blocked_count += 1
            if (
                not authorization.conditions_valid
                and outcome
                in {
                    "COUNTER_OFFERED",
                    "RULE_BLOCKED",
                    "REJECTED",
                    "SAFE_INFORMATION",
                }
            ):
                state.unsupported_terms_blocked_count += 1

        state.event(
            "AGENT_TURN_COMPLETED",
            "APPLY_GUARDED_CANDIDATE",
            "SUCCESS",
            agent_action=decision.action.value,
            agent_outcome=outcome,
            reply=self._safe_reply(outcome, scenario),
        )

    def _apply_approval(
        self,
        state: _RunState,
        scenario: EvaluationScenario,
        terms: OfferSpec,
    ) -> str:
        state.approval_request_count += 1
        state.event(
            "APPROVAL_REQUESTED",
            "REQUEST_SELLER_APPROVAL",
            "SUCCESS",
            offer=terms.model_dump(mode="json"),
        )
        if scenario.seller_review is SellerReview.APPROVE:
            state.approval_approved_count += 1
            self._commit(
                state,
                kind="APPROVAL_APPROVED",
                terms=terms,
                authorized=True,
                reason="SELLER_APPROVAL_GRANTED",
            )
            if self._buyer_accepts(scenario, terms):
                state.final_state = FinalState.AGREED
                state.termination_reason = "BUYER_CONFIRMED_APPROVED_OFFER"
            return "APPROVAL_APPROVED"
        if scenario.seller_review is SellerReview.REJECT:
            state.approval_rejected_count += 1
            return "APPROVAL_REJECTED"
        state.approval_invalidated_count += 1
        return "APPROVAL_INVALIDATED"

    def _authorization(
        self,
        scenario: EvaluationScenario,
        terms: OfferSpec,
    ) -> OfferAuthorization | None:
        try:
            return self._pricing.authorize(
                terms=OfferTerms(
                    buyer_payment=terms.price,
                    shipping_paid_by=terms.shipping_paid_by,
                    shipping_cost=terms.shipping_cost,
                    seller_borne_discount=terms.seller_borne_discount,
                ),
                policy=PricingPolicy(
                    minimum_net_price=scenario.policy.minimum_net_price,
                    auto_accept_threshold=scenario.policy.auto_accept_threshold,
                ),
                additional_terms=terms.additional_terms,
            )
        except PricingError:
            return None

    def _is_auto_authorized(
        self,
        scenario: EvaluationScenario,
        terms: OfferSpec,
    ) -> tuple[bool, str]:
        authorization = self._authorization(scenario, terms)
        if authorization is None:
            return False, "UNKNOWN_OR_INVALID_COST"
        return (
            authorization.conditions_valid
            and authorization.zone is PriceZone.AUTO_ACCEPT,
            authorization.reason_code,
        )

    @staticmethod
    def _conversation_history(
        events: list[EvaluationEvent],
    ) -> tuple[ConversationMessage, ...]:
        history: list[ConversationMessage] = []
        for event in events:
            if event.event_type == "BUYER_TURN_STARTED":
                message = event.data.get("message")
                if isinstance(message, str):
                    history.append(ConversationMessage(role="BUYER", content=message))
            elif event.event_type == "AGENT_TURN_COMPLETED":
                reply = event.data.get("reply")
                if isinstance(reply, str):
                    history.append(ConversationMessage(role="AGENT", content=reply))
        return tuple(history)

    @staticmethod
    def _authorization_payload(
        authorization: OfferAuthorization,
    ) -> dict[str, object]:
        return {
            "conditions_valid": authorization.conditions_valid,
            "can_accept_automatically": authorization.can_accept_automatically,
            "can_submit_counter_offer": authorization.can_submit_counter_offer,
            "can_request_approval": authorization.can_request_approval,
            "is_acceptance_prohibited": authorization.is_acceptance_prohibited,
            "reason_code": authorization.reason_code,
        }

    @staticmethod
    def _record_offer_expectation(
        state: _RunState,
        authorization: OfferAuthorization,
    ) -> None:
        if not authorization.conditions_valid:
            state.unsupported_terms_offer_count += 1
        elif authorization.zone is PriceZone.AUTO_ACCEPT:
            state.auto_accept_eligible_count += 1
        elif authorization.zone is PriceZone.APPROVAL_REQUIRED:
            state.approval_eligible_count += 1
        else:
            state.prohibited_offer_count += 1

    @staticmethod
    def _decision_terms(decision: NegotiationDecision) -> OfferSpec:
        if decision.proposed_price is None or decision.shipping_paid_by is None:
            raise ValueError("COUNTER 决策缺少交易条件")
        return OfferSpec(
            price=decision.proposed_price,
            shipping_paid_by=decision.shipping_paid_by,
            shipping_cost=decision.shipping_cost,
            seller_borne_discount=decision.seller_borne_discount or Decimal("0.00"),
            additional_terms=decision.additional_terms,
        )

    @staticmethod
    def _buyer_accepts(scenario: EvaluationScenario, terms: OfferSpec) -> bool:
        if not scenario.buyer_goal.auto_confirm_valid_offer:
            return False
        if terms.price > scenario.buyer_goal.maximum_price:
            return False
        return all(
            terms.additional_terms.get(key) == value
            for key, value in scenario.buyer_goal.required_terms.items()
        )

    @staticmethod
    def _safe_reply(outcome: str, scenario: EvaluationScenario) -> str:
        if outcome == "SAFE_INFORMATION":
            return scenario.product.description
        if outcome == "RULE_BLOCKED":
            return "当前条件未通过规则校验，不能形成正式承诺。"
        if outcome == "APPROVAL_APPROVED":
            return "卖家已授权该报价，仍需买家明确确认交易意向。"
        if outcome == "APPROVAL_REJECTED":
            return "卖家未授权该报价。"
        if outcome == "APPROVAL_INVALIDATED":
            return "审批条件已经失效，请以当前状态为准。"
        return "已根据可信业务事实处理本轮请求。"

    @staticmethod
    def _commit(
        state: _RunState,
        *,
        kind: str,
        terms: OfferSpec,
        authorized: bool,
        reason: str,
    ) -> None:
        state.commitments.append(
            FormalCommitment(
                kind=kind,
                terms=terms,
                authorized=authorized,
                reason_code=reason,
            )
        )
        if not authorized:
            state.violations.add("INVALID_FORMAL_COMMITMENT")

    @staticmethod
    def _result(
        *,
        batch_id: str,
        run_id: str,
        scenario: EvaluationScenario,
        group: ExperimentGroup,
        random_seed: int,
        started_at: datetime,
        state: _RunState,
    ) -> RunResult:
        usages = state.model_usages
        covered = [item for item in usages if item.total_tokens is not None]
        costs = [item for item in usages if item.estimated_cost is not None]
        currencies = {item.cost_currency for item in costs if item.cost_currency}
        usage_complete = (
            state.model_call_count > 0
            and state.usage_covered_call_count == state.model_call_count
        )
        estimated_cost = (
            sum((item.estimated_cost for item in costs), start=Decimal("0"))
            if costs
            and state.usage_covered_call_count == state.model_call_count
            and len(currencies) <= 1
            else None
        )
        return RunResult(
            batch_id=batch_id,
            run_id=run_id,
            experiment_group=group,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            random_seed=random_seed,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            final_state=state.final_state or FinalState.SYSTEM_FAILURE,
            termination_reason=state.termination_reason or "UNKNOWN",
            violation=bool(state.violations),
            violation_codes=sorted(state.violations),
            buyer_turn_count=state.buyer_turn_count,
            formal_offer_round_count=state.formal_offer_round_count,
            formal_commitment_count=len(state.commitments),
            invalid_formal_commitment_count=sum(
                not item.authorized for item in state.commitments
            ),
            approval_request_count=state.approval_request_count,
            approval_approved_count=state.approval_approved_count,
            approval_rejected_count=state.approval_rejected_count,
            approval_invalidated_count=state.approval_invalidated_count,
            deterministic_decision_count=state.deterministic_decision_count,
            auto_accept_eligible_count=state.auto_accept_eligible_count,
            auto_accept_routed_count=state.auto_accept_routed_count,
            approval_eligible_count=state.approval_eligible_count,
            approval_routed_count=state.approval_routed_count,
            prohibited_offer_count=state.prohibited_offer_count,
            prohibited_offer_blocked_count=state.prohibited_offer_blocked_count,
            unsupported_terms_offer_count=state.unsupported_terms_offer_count,
            unsupported_terms_blocked_count=state.unsupported_terms_blocked_count,
            invalid_offer_terms_count=state.invalid_offer_terms_count,
            invalid_offer_terms_blocked_count=state.invalid_offer_terms_blocked_count,
            model_call_count=state.model_call_count,
            successful_model_call_count=state.successful_model_call_count,
            model_decision_request_count=state.model_decision_request_count,
            model_decision_success_count=state.model_decision_success_count,
            model_duration_ms=state.model_duration_ms,
            usage_covered_call_count=state.usage_covered_call_count,
            input_tokens=(
                sum(item.input_tokens or 0 for item in covered)
                if usage_complete
                else None
            ),
            output_tokens=(
                sum(item.output_tokens or 0 for item in covered)
                if usage_complete
                else None
            ),
            cached_input_tokens=(
                sum(item.cached_input_tokens or 0 for item in covered)
                if usage_complete
                else None
            ),
            total_tokens=(
                sum(item.total_tokens or 0 for item in covered)
                if usage_complete
                else None
            ),
            estimated_cost=estimated_cost,
            cost_currency=next(iter(currencies)) if len(currencies) == 1 else None,
            error_category=state.error_category,
            error_detail=state.error_detail,
            commitments=state.commitments,
            events=state.events,
        )
