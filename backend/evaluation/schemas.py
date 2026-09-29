import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.agent.decision import NegotiationDecision
from app.agent.model_observation import ProviderUsage
from app.services.pricing_service import ShippingPayer


class ExperimentGroup(StrEnum):
    PROMPT_ONLY = "A"
    RULE_ENGINE = "B"
    FULL_WORKFLOW = "C"


class ScenarioCategory(StrEnum):
    INQUIRY = "INQUIRY"
    NORMAL_OFFER = "NORMAL_OFFER"
    LOW_OFFER = "LOW_OFFER"
    MULTI_ROUND = "MULTI_ROUND"
    ADDITIONAL_TERMS = "ADDITIONAL_TERMS"
    APPROVAL_OFFER = "APPROVAL_OFFER"
    MALICIOUS_INSTRUCTION = "MALICIOUS_INSTRUCTION"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


class SellerReview(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EXPIRE = "EXPIRE"


class ExhaustionBehavior(StrEnum):
    CLOSE = "CLOSE"
    LEAVE_OPEN = "LEAVE_OPEN"


class FinalState(StrEnum):
    AGREED = "AGREED"
    CLOSED_VALID = "CLOSED_VALID"
    UNRESOLVED = "UNRESOLVED"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


class OfferSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    shipping_paid_by: ShippingPayer
    shipping_cost: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    seller_borne_discount: Decimal = Field(
        default=Decimal("0.00"),
        ge=0,
        decimal_places=2,
    )
    additional_terms: dict[str, JsonValue] = Field(default_factory=dict)


class ProductFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    listed_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class PolicyFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_net_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    auto_accept_threshold: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    max_rounds: int = Field(default=6, ge=1, le=50)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "PolicyFacts":
        if self.auto_accept_threshold < self.minimum_net_price:
            raise ValueError("自动接受阈值不能低于最低净收入")
        return self


class BuyerGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    required_terms: dict[str, JsonValue] = Field(default_factory=dict)
    auto_confirm_valid_offer: bool = True


class BuyerTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000)
    offer: OfferSpec | None = None


class EvaluationScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    scenario_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    category: ScenarioCategory
    product: ProductFacts
    policy: PolicyFacts
    buyer_goal: BuyerGoal
    turns: list[BuyerTurn] = Field(min_length=1, max_length=50)
    max_turns: int = Field(ge=1, le=50)
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    seller_review: SellerReview = SellerReview.APPROVE
    exhaustion_behavior: ExhaustionBehavior = ExhaustionBehavior.CLOSE
    tags: list[str] = Field(default_factory=list)
    synthetic_data: bool = True

    @model_validator(mode="after")
    def validate_turn_budget(self) -> "EvaluationScenario":
        if len(self.turns) > self.max_turns:
            raise ValueError("场景轮次不能超过 max_turns")
        if not self.synthetic_data:
            raise ValueError("阶段七只允许合成评测数据")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("场景标签不能重复")
        if any(re.fullmatch(r"[a-z0-9][a-z0-9_]{0,39}", tag) is None for tag in self.tags):
            raise ValueError("场景标签只能包含字母、数字和下划线")
        return self


class EvaluationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_samples: int = Field(default=300, ge=1, le=10000)
    max_model_calls: int = Field(default=300, ge=1, le=100000)
    max_tokens: int = Field(default=200000, ge=1)
    max_estimated_cost: Decimal | None = Field(default=None, ge=0)
    timeout_seconds: int = Field(default=900, ge=1, le=86400)


class ModelResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    decision: NegotiationDecision
    usage: ProviderUsage
    duration_ms: int = Field(ge=0)
    provider_attempt_count: int = Field(default=1, ge=1, le=2)


class EvaluationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_index: int = Field(ge=0)
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    action: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    outcome: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,31}$")
    occurred_at: datetime
    data: dict[str, JsonValue] = Field(default_factory=dict)


class FormalCommitment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    terms: OfferSpec | None = None
    authorized: bool
    reason_code: str


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1.0.0"
    result_schema_version: str = "1.2.0"
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    run_id: str
    experiment_group: ExperimentGroup
    scenario_id: str
    scenario_version: str
    random_seed: int
    started_at: datetime
    completed_at: datetime
    final_state: FinalState
    termination_reason: str
    violation: bool
    violation_codes: list[str]
    buyer_turn_count: int
    formal_offer_round_count: int
    formal_commitment_count: int
    invalid_formal_commitment_count: int
    approval_request_count: int
    approval_approved_count: int
    approval_rejected_count: int
    approval_invalidated_count: int
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
    manual_recovery_count: int = 0
    model_call_count: int
    successful_model_call_count: int = 0
    model_decision_request_count: int = 0
    model_decision_success_count: int = 0
    model_duration_ms: int = 0
    usage_covered_call_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    total_tokens: int | None
    estimated_cost: Decimal | None
    cost_currency: str | None
    error_category: str | None
    error_detail: str | None = Field(default=None, max_length=2000)
    commitments: list[FormalCommitment]
    events: list[EvaluationEvent]


class BatchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1.0.0"
    result_schema_version: str = "1.2.0"
    batch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    git_commit: str
    git_worktree_dirty: bool
    application_version: str
    scenario_set_version: str
    scenario_set_hash: str
    scenario_ids: list[str]
    scenario_count: int
    scenario_tags: list[str] = Field(default_factory=list)
    groups: list[ExperimentGroup]
    random_seed: int
    model_provider: str
    model_name: str
    model_is_mock: bool
    structured_output: bool
    thinking_enabled: bool
    model_temperature: float
    model_timeout_seconds: float
    model_max_retries: int
    model_input_price_per_million: Decimal | None = None
    model_output_price_per_million: Decimal | None = None
    model_cached_input_price_per_million: Decimal | None = None
    model_cost_currency: str | None = None
    prompt_version: str
    prompt_hash: str
    reply_policy_version: str
    budget: EvaluationBudget
    started_at: datetime
    completed_at: datetime | None = None
    python_version: str
    database_type: str = "none-in-memory"
    migration_version: str = "not-applicable"
    preflight_failure_count: int = 0
    started_run_count: int = 0
    failed_run_count: int = 0
