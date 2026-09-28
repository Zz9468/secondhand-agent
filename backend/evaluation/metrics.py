from collections import Counter
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from evaluation.schemas import ExperimentGroup, FinalState, RunResult


class RatioMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numerator: int
    denominator: int
    value: float | None


class GroupMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_group: ExperimentGroup
    started_run_count: int
    final_state_counts: dict[str, int]
    intent_agreement_rate: RatioMetric
    valid_termination_rate: RatioMetric
    violating_run_rate: RatioMetric
    invalid_formal_commitment_rate: RatioMetric
    approval_run_rate: RatioMetric
    usage_coverage_rate: RatioMetric
    average_negotiation_turns: float | None
    formal_offer_round_count: int
    model_call_count: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    total_tokens: int | None
    estimated_cost_by_currency: dict[str, Decimal]
    system_failure_model_call_count: int
    system_failure_total_tokens: int | None


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str = "1.0.0"
    result_schema_version: str = "1.0.0"
    groups: list[GroupMetrics]


def _ratio(numerator: int, denominator: int) -> RatioMetric:
    return RatioMetric(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator else None,
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
    for group in ExperimentGroup:
        group_runs = [run for run in runs if run.experiment_group is group]
        started = len(group_runs)
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
        for run in group_runs:
            if run.estimated_cost is None or run.cost_currency is None:
                continue
            costs[run.cost_currency] = (
                costs.get(run.cost_currency, Decimal("0")) + run.estimated_cost
            )
        failed_runs = [
            run for run in group_runs if run.final_state is FinalState.SYSTEM_FAILURE
        ]
        groups.append(
            GroupMetrics(
                experiment_group=group,
                started_run_count=started,
                final_state_counts=dict(sorted(final_states.items())),
                intent_agreement_rate=_ratio(agreements, started),
                valid_termination_rate=_ratio(valid_terminations, started),
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
                usage_coverage_rate=_ratio(usage_covered_calls, model_calls),
                average_negotiation_turns=(
                    sum(run.buyer_turn_count for run in group_runs) / started
                    if started
                    else None
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
