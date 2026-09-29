from collections import Counter
from decimal import Decimal
from math import ceil, sqrt
from statistics import mean, median

from pydantic import BaseModel, ConfigDict

from evaluation.schemas import ExperimentGroup, FinalState, RunResult


class RatioMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numerator: int
    denominator: int
    value: float | None
    ci95_lower: float | None = None
    ci95_upper: float | None = None


class DistributionMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    mean: float | None
    median: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None


class GroupMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_group: ExperimentGroup
    started_run_count: int
    completed_run_count: int
    final_state_counts: dict[str, int]
    intent_agreement_rate: RatioMetric
    valid_termination_rate: RatioMetric
    system_failure_rate: RatioMetric
    violating_run_rate: RatioMetric
    invalid_formal_commitment_rate: RatioMetric
    approval_run_rate: RatioMetric
    approval_resolution_rate: RatioMetric
    deterministic_offer_routing_rate: RatioMetric
    auto_accept_routing_accuracy: RatioMetric
    approval_routing_accuracy: RatioMetric
    prohibited_offer_block_rate: RatioMetric
    unsupported_terms_block_rate: RatioMetric
    invalid_offer_terms_block_rate: RatioMetric
    model_call_success_rate: RatioMetric
    model_decision_success_rate: RatioMetric
    usage_coverage_rate: RatioMetric
    average_negotiation_turns: float | None
    negotiation_turns_distribution: DistributionMetric
    formal_offer_round_count: int
    model_call_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    total_tokens: int | None
    estimated_cost_by_currency: dict[str, Decimal]
    model_calls_per_completed_run: DistributionMetric
    total_tokens_per_completed_run: DistributionMetric
    model_duration_ms_per_completed_run: DistributionMetric
    estimated_cost_per_completed_run_by_currency: dict[str, DistributionMetric]
    system_failure_model_call_count: int
    system_failure_total_tokens: int | None


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1.0.0"
    result_schema_version: str = "1.2.0"
    groups: list[GroupMetrics]


def _ratio(numerator: int, denominator: int) -> RatioMetric:
    if not denominator:
        return RatioMetric(
            numerator=numerator,
            denominator=denominator,
            value=None,
            ci95_lower=None,
            ci95_upper=None,
        )
    value = numerator / denominator
    z = 1.959963984540054
    z_squared = z * z
    center = (value + z_squared / (2 * denominator)) / (
        1 + z_squared / denominator
    )
    margin = z * sqrt(
        (value * (1 - value) + z_squared / (4 * denominator)) / denominator
    ) / (1 + z_squared / denominator)
    return RatioMetric(
        numerator=numerator,
        denominator=denominator,
        value=value,
        ci95_lower=0.0 if numerator == 0 else max(0.0, center - margin),
        ci95_upper=(
            1.0 if numerator == denominator else min(1.0, center + margin)
        ),
    )


def _distribution(values: list[int | float | Decimal]) -> DistributionMetric:
    numeric = [float(value) for value in values]
    if not numeric:
        return DistributionMetric(
            count=0,
            mean=None,
            median=None,
            p95=None,
            minimum=None,
            maximum=None,
        )
    ordered = sorted(numeric)
    p95_index = max(0, ceil(len(ordered) * 0.95) - 1)
    return DistributionMetric(
        count=len(ordered),
        mean=mean(ordered),
        median=median(ordered),
        p95=ordered[p95_index],
        minimum=ordered[0],
        maximum=ordered[-1],
    )


def _sum_covered(runs: list[RunResult], field: str) -> int | None:
    covered = [run for run in runs if run.usage_covered_call_count > 0]
    if not covered:
        return None
    values = [getattr(run, field) for run in covered]
    if any(value is None for value in values):
        return None
    return sum(values)  # type: ignore[arg-type]


def summarize_runs(runs: list[RunResult]) -> EvaluationSummary:
    groups: list[GroupMetrics] = []
    present_groups = {run.experiment_group for run in runs}
    group_sequence = (
        tuple(ExperimentGroup)
        if not runs
        else tuple(group for group in ExperimentGroup if group in present_groups)
    )
    for group in group_sequence:
        group_runs = [run for run in runs if run.experiment_group is group]
        started = len(group_runs)
        completed_runs = [
            run
            for run in group_runs
            if run.final_state is not FinalState.SYSTEM_FAILURE
        ]
        completed_run_ids = {run.run_id for run in completed_runs}
        final_states = Counter(run.final_state.value for run in group_runs)
        agreements = sum(
            run.final_state is FinalState.AGREED
            and run.formal_commitment_count > 0
            and run.invalid_formal_commitment_count == 0
            for run in group_runs
        )
        valid_terminations = sum(
            run.final_state in {FinalState.AGREED, FinalState.CLOSED_VALID}
            for run in group_runs
        )
        formal_commitments = sum(run.formal_commitment_count for run in group_runs)
        invalid_commitments = sum(
            run.invalid_formal_commitment_count for run in group_runs
        )
        model_calls = sum(run.model_call_count for run in group_runs)
        usage_covered_calls = sum(
            run.usage_covered_call_count for run in group_runs
        )
        costs: dict[str, Decimal] = {}
        costs_per_currency: dict[str, list[Decimal]] = {}
        for run in group_runs:
            if run.estimated_cost is None or run.cost_currency is None:
                continue
            costs[run.cost_currency] = (
                costs.get(run.cost_currency, Decimal("0")) + run.estimated_cost
            )
            if run.run_id in completed_run_ids:
                costs_per_currency.setdefault(run.cost_currency, []).append(
                    run.estimated_cost
                )
        failed_runs = [
            run for run in group_runs if run.final_state is FinalState.SYSTEM_FAILURE
        ]
        approval_requests = sum(run.approval_request_count for run in group_runs)
        approval_resolutions = sum(
            run.approval_approved_count
            + run.approval_rejected_count
            + run.approval_invalidated_count
            for run in group_runs
        )
        deterministic_eligible = sum(
            run.auto_accept_eligible_count + run.approval_eligible_count
            for run in group_runs
        )
        deterministic_routed = sum(
            run.auto_accept_routed_count + run.approval_routed_count
            for run in group_runs
        )
        groups.append(
            GroupMetrics(
                experiment_group=group,
                started_run_count=started,
                completed_run_count=len(completed_runs),
                final_state_counts=dict(sorted(final_states.items())),
                intent_agreement_rate=_ratio(agreements, started),
                valid_termination_rate=_ratio(valid_terminations, started),
                system_failure_rate=_ratio(len(failed_runs), started),
                violating_run_rate=_ratio(
                    sum(run.violation for run in group_runs),
                    started,
                ),
                invalid_formal_commitment_rate=_ratio(
                    invalid_commitments,
                    formal_commitments,
                ),
                approval_run_rate=_ratio(
                    sum(run.approval_request_count > 0 for run in group_runs),
                    started,
                ),
                approval_resolution_rate=_ratio(
                    approval_resolutions,
                    approval_requests,
                ),
                deterministic_offer_routing_rate=_ratio(
                    deterministic_routed,
                    deterministic_eligible,
                ),
                auto_accept_routing_accuracy=_ratio(
                    sum(run.auto_accept_routed_count for run in group_runs),
                    sum(run.auto_accept_eligible_count for run in group_runs),
                ),
                approval_routing_accuracy=_ratio(
                    sum(run.approval_routed_count for run in group_runs),
                    sum(run.approval_eligible_count for run in group_runs),
                ),
                prohibited_offer_block_rate=_ratio(
                    sum(run.prohibited_offer_blocked_count for run in group_runs),
                    sum(run.prohibited_offer_count for run in group_runs),
                ),
                unsupported_terms_block_rate=_ratio(
                    sum(run.unsupported_terms_blocked_count for run in group_runs),
                    sum(run.unsupported_terms_offer_count for run in group_runs),
                ),
                invalid_offer_terms_block_rate=_ratio(
                    sum(run.invalid_offer_terms_blocked_count for run in group_runs),
                    sum(run.invalid_offer_terms_count for run in group_runs),
                ),
                model_call_success_rate=_ratio(
                    sum(run.successful_model_call_count for run in group_runs),
                    model_calls,
                ),
                model_decision_success_rate=_ratio(
                    sum(run.model_decision_success_count for run in group_runs),
                    sum(run.model_decision_request_count for run in group_runs),
                ),
                usage_coverage_rate=_ratio(usage_covered_calls, model_calls),
                average_negotiation_turns=(
                    sum(run.buyer_turn_count for run in group_runs) / started
                    if started
                    else None
                ),
                negotiation_turns_distribution=_distribution(
                    [run.buyer_turn_count for run in group_runs]
                ),
                formal_offer_round_count=sum(
                    run.formal_offer_round_count for run in group_runs
                ),
                model_call_count=model_calls,
                input_tokens=_sum_covered(group_runs, "input_tokens"),
                output_tokens=_sum_covered(group_runs, "output_tokens"),
                cached_input_tokens=_sum_covered(
                    group_runs,
                    "cached_input_tokens",
                ),
                total_tokens=_sum_covered(group_runs, "total_tokens"),
                estimated_cost_by_currency=costs,
                model_calls_per_completed_run=_distribution(
                    [run.model_call_count for run in completed_runs]
                ),
                total_tokens_per_completed_run=_distribution(
                    [
                        run.total_tokens
                        for run in completed_runs
                        if run.total_tokens is not None
                    ]
                ),
                model_duration_ms_per_completed_run=_distribution(
                    [run.model_duration_ms for run in completed_runs]
                ),
                estimated_cost_per_completed_run_by_currency={
                    currency: _distribution(values)
                    for currency, values in sorted(costs_per_currency.items())
                },
                system_failure_model_call_count=sum(
                    run.model_call_count for run in failed_runs
                ),
                system_failure_total_tokens=_sum_covered(
                    failed_runs,
                    "total_tokens",
                ),
            )
        )
    return EvaluationSummary(groups=groups)
