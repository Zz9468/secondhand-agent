import json
from pathlib import Path

import pytest

from app.agent.model_observation import ProviderUsage
from app.core.config import Settings
from evaluation.artifacts import write_batch_artifacts
from evaluation.metrics import summarize_runs
from evaluation.models import (
    GROUP_PROMPTS,
    EvaluationModelError,
    ScriptedEvaluationModel,
)
from evaluation.runner import EvaluationRunner
from evaluation.scenarios import load_scenarios
from evaluation.schemas import (
    EvaluationBudget,
    ExperimentGroup,
    FinalState,
    ScenarioCategory,
)


def _settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite://")


def _run_all():
    return EvaluationRunner(
        model=ScriptedEvaluationModel(),
        settings=_settings(),
    ).run(
        scenario_set=load_scenarios(),
        budget=EvaluationBudget(max_samples=24),
        batch_id="unit-stage7",
    )


class _UsageReportingFailureModel:
    provider = "synthetic-provider"
    model_name = "synthetic-failure"
    is_mock = True

    def decide(self, _request: object) -> None:
        raise EvaluationModelError(
            "STRUCTURED_OUTPUT_ERROR",
            usage=ProviderUsage(
                provider=self.provider,
                model_name=self.model_name,
                input_tokens=100,
                output_tokens=20,
                cached_input_tokens=0,
                total_tokens=120,
            ),
            duration_ms=7,
        )


def test_scenario_set_is_versioned_unique_and_synthetic() -> None:
    scenario_set = load_scenarios()

    assert scenario_set.version == "1.0.0"
    assert len(scenario_set.content_hash) == 64
    assert len(scenario_set.scenarios) == 8
    assert {scenario.category for scenario in scenario_set.scenarios} == set(
        ScenarioCategory
    )
    assert all(scenario.synthetic_data for scenario in scenario_set.scenarios)
    assert all(
        "COUNTER：offer_id 必须为空" in prompt
        and "其他动作的 dialogue_acts 必须为空数组" in prompt
        for prompt in GROUP_PROMPTS.values()
    )


def test_three_groups_share_inputs_and_continue_after_system_failure() -> None:
    result = _run_all()

    assert result.manifest.started_run_count == 24
    assert result.manifest.failed_run_count == 3
    assert result.manifest.database_type == "none-in-memory"
    assert result.manifest.migration_version == "not-applicable"
    assert len(result.runs) == 24

    for scenario_id in result.manifest.scenario_ids:
        scenario_runs = [
            run for run in result.runs if run.scenario_id == scenario_id
        ]
        assert {run.experiment_group for run in scenario_runs} == set(
            ExperimentGroup
        )
        fingerprints = {
            run.events[0].data.get("buyer_input_fingerprint")
            for run in scenario_runs
        }
        assert len(fingerprints) == 1

    failures = [
        run for run in result.runs if run.final_state is FinalState.SYSTEM_FAILURE
    ]
    assert {run.experiment_group for run in failures} == set(ExperimentGroup)
    assert {run.error_category for run in failures} == {"MODEL_TIMEOUT"}


def test_guarded_groups_block_violations_and_full_workflow_uses_approval() -> None:
    result = _run_all()

    prompt_only = [
        run
        for run in result.runs
        if run.experiment_group is ExperimentGroup.PROMPT_ONLY
    ]
    guarded = [
        run
        for run in result.runs
        if run.experiment_group
        in {ExperimentGroup.RULE_ENGINE, ExperimentGroup.FULL_WORKFLOW}
    ]
    assert any(run.violation for run in prompt_only)
    assert not any(run.violation for run in guarded)

    approval_runs = [
        run
        for run in result.runs
        if run.scenario_id == "seller_approval_zone_offer"
    ]
    by_group = {run.experiment_group: run for run in approval_runs}
    assert by_group[ExperimentGroup.RULE_ENGINE].final_state is FinalState.UNRESOLVED
    assert by_group[ExperimentGroup.FULL_WORKFLOW].final_state is FinalState.AGREED
    assert by_group[ExperimentGroup.FULL_WORKFLOW].approval_request_count == 1
    assert by_group[ExperimentGroup.FULL_WORKFLOW].approval_approved_count == 1


def test_metrics_keep_frozen_numerators_denominators_and_null_zero_denominator() -> None:
    result = _run_all()
    by_group = {
        item.experiment_group: item for item in result.summary.groups
    }

    assert by_group[ExperimentGroup.PROMPT_ONLY].violating_run_rate.numerator > 0
    assert by_group[ExperimentGroup.PROMPT_ONLY].violating_run_rate.denominator == 8
    assert by_group[ExperimentGroup.RULE_ENGINE].violating_run_rate.value == 0
    assert by_group[ExperimentGroup.FULL_WORKFLOW].approval_run_rate.numerator > 0

    empty = summarize_runs([])
    assert all(item.intent_agreement_rate.value is None for item in empty.groups)
    assert all(
        item.invalid_formal_commitment_rate.value is None
        for item in empty.groups
    )


def test_budget_preflight_rejects_batch_before_any_run() -> None:
    runner = EvaluationRunner(
        model=ScriptedEvaluationModel(),
        settings=_settings(),
    )

    with pytest.raises(ValueError, match="样本数 24 超过预算 23"):
        runner.run(
            scenario_set=load_scenarios(),
            budget=EvaluationBudget(max_samples=23),
            batch_id="over-budget",
        )


def test_token_budget_records_the_call_that_crossed_the_limit() -> None:
    result = EvaluationRunner(
        model=ScriptedEvaluationModel(),
        settings=_settings(),
    ).run(
        scenario_set=load_scenarios(),
        scenario_ids={"public_product_inquiry"},
        budget=EvaluationBudget(
            max_samples=3,
            max_model_calls=3,
            max_tokens=1,
        ),
        batch_id="token-budget",
    )

    first, *remaining = result.runs
    assert first.final_state is FinalState.SYSTEM_FAILURE
    assert first.error_category == "TOKEN_BUDGET_EXHAUSTED"
    assert first.model_call_count == 1
    assert first.usage_covered_call_count == 1
    assert first.total_tokens is not None
    assert all(run.model_call_count == 0 for run in remaining)
    assert all(
        run.error_category == "TOKEN_BUDGET_EXHAUSTED" for run in remaining
    )


def test_failed_model_call_preserves_reported_usage_and_cost() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite://",
        model_input_price_per_million="1",
        model_output_price_per_million="2",
        model_cost_currency="CNY",
    )
    result = EvaluationRunner(
        model=_UsageReportingFailureModel(),  # type: ignore[arg-type]
        settings=settings,
    ).run(
        scenario_set=load_scenarios(),
        groups=(ExperimentGroup.FULL_WORKFLOW,),
        scenario_ids={"near_listed_price_offer"},
        budget=EvaluationBudget(max_samples=1),
        batch_id="failed-usage",
    )

    run = result.runs[0]
    assert run.final_state is FinalState.SYSTEM_FAILURE
    assert run.model_call_count == 1
    assert run.usage_covered_call_count == 1
    assert run.total_tokens == 120
    assert str(run.estimated_cost) == "0.00014000"
    assert run.events[2].data["total_tokens"] == 120


def test_artifacts_are_machine_readable_and_do_not_contain_secrets(
    tmp_path: Path,
) -> None:
    result = _run_all()
    batch_dir = write_batch_artifacts(result, tmp_path)

    assert {path.name for path in batch_dir.iterdir()} == {
        "events.jsonl",
        "manifest.json",
        "runs.jsonl",
        "summary.json",
    }
    manifest = json.loads((batch_dir / "manifest.json").read_text("utf-8"))
    runs = [
        json.loads(line)
        for line in (batch_dir / "runs.jsonl").read_text("utf-8").splitlines()
    ]
    events = [
        json.loads(line)
        for line in (batch_dir / "events.jsonl").read_text("utf-8").splitlines()
    ]
    assert manifest["batch_id"] == "unit-stage7"
    assert len(runs) == 24
    assert len(events) > len(runs)
    assert all("events" not in run for run in runs)
    serialized = json.dumps(manifest, ensure_ascii=False).lower()
    assert "api_key" not in serialized
    assert "database_url" not in serialized

    with pytest.raises(FileExistsError):
        write_batch_artifacts(result, tmp_path)


def test_artifact_writer_rejects_unsafe_batch_id(tmp_path: Path) -> None:
    result = _run_all()
    unsafe_manifest = result.manifest.model_copy(update={"batch_id": "../escape"})
    unsafe_result = type(result)(
        manifest=unsafe_manifest,
        runs=result.runs,
        summary=result.summary,
    )

    with pytest.raises(ValueError, match="batch_id"):
        write_batch_artifacts(unsafe_result, tmp_path)
