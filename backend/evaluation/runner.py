import platform
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from evaluation.adapters import BudgetTracker, IsolatedExperimentAdapter
from evaluation.metrics import EvaluationSummary, summarize_runs
from evaluation.models import PROMPT_VERSION, EvaluationModel, prompt_hash
from evaluation.scenarios import ScenarioSet
from evaluation.schemas import (
    BatchManifest,
    EvaluationBudget,
    EvaluationEvent,
    EvaluationScenario,
    ExperimentGroup,
    FinalState,
    RunResult,
)


@dataclass(frozen=True, slots=True)
class BatchResult:
    manifest: BatchManifest
    runs: list[RunResult]
    summary: EvaluationSummary


class EvaluationRunner:
    """在无数据库依赖的隔离适配器中公平运行 A/B/C 三组实验。"""

    def __init__(self, *, model: EvaluationModel, settings: Settings) -> None:
        self._model = model
        self._settings = settings
        self._adapter = IsolatedExperimentAdapter(model=model, settings=settings)

    def run(
        self,
        *,
        scenario_set: ScenarioSet,
        groups: tuple[ExperimentGroup, ...] = tuple(ExperimentGroup),
        random_seed: int = 20260928,
        budget: EvaluationBudget | None = None,
        batch_id: str | None = None,
        scenario_ids: set[str] | None = None,
    ) -> BatchResult:
        selected = [
            scenario
            for scenario in scenario_set.scenarios
            if scenario_ids is None or scenario.scenario_id in scenario_ids
        ]
        if scenario_ids is not None:
            missing = scenario_ids - {scenario.scenario_id for scenario in selected}
            if missing:
                raise ValueError(f"未知场景 ID: {', '.join(sorted(missing))}")
        if not selected:
            raise ValueError("至少需要选择一个评测场景")
        if not groups or len(groups) != len(set(groups)):
            raise ValueError("实验组不能为空或重复")

        effective_budget = budget or EvaluationBudget()
        sample_count = len(selected) * len(groups)
        if sample_count > effective_budget.max_samples:
            raise ValueError(
                f"样本数 {sample_count} 超过预算 {effective_budget.max_samples}"
            )

        effective_batch_id = batch_id or self._new_batch_id()
        started_at = datetime.now(UTC)
        deadline = time.monotonic() + effective_budget.timeout_seconds
        manifest = self._manifest(
            batch_id=effective_batch_id,
            scenario_set=scenario_set,
            scenarios=selected,
            groups=groups,
            random_seed=random_seed,
            budget=effective_budget,
            started_at=started_at,
        )
        tracker = BudgetTracker(
            max_model_calls=effective_budget.max_model_calls,
            max_tokens=effective_budget.max_tokens,
            max_estimated_cost=effective_budget.max_estimated_cost,
            cost_currency=self._settings.model_cost_currency,
            deadline_monotonic=deadline,
        )
        runs: list[RunResult] = []
        for scenario in selected:
            for group in groups:
                try:
                    run = self._adapter.run(
                        batch_id=effective_batch_id,
                        scenario=scenario,
                        group=group,
                        random_seed=random_seed,
                        budget=tracker,
                    )
                except Exception as exc:
                    run = self._unexpected_failure(
                        batch_id=effective_batch_id,
                        scenario=scenario,
                        group=group,
                        random_seed=random_seed,
                        error_category=type(exc).__name__.upper(),
                    )
                runs.append(run)

        completed_at = datetime.now(UTC)
        manifest = manifest.model_copy(
            update={
                "completed_at": completed_at,
                "started_run_count": len(runs),
                "failed_run_count": sum(
                    run.final_state is FinalState.SYSTEM_FAILURE for run in runs
                ),
            }
        )
        return BatchResult(
            manifest=manifest,
            runs=runs,
            summary=summarize_runs(runs),
        )

    def _manifest(
        self,
        *,
        batch_id: str,
        scenario_set: ScenarioSet,
        scenarios: list[EvaluationScenario],
        groups: tuple[ExperimentGroup, ...],
        random_seed: int,
        budget: EvaluationBudget,
        started_at: datetime,
    ) -> BatchManifest:
        commit, dirty = self._git_state()
        return BatchManifest(
            batch_id=batch_id,
            git_commit=commit,
            git_worktree_dirty=dirty,
            application_version=self._application_version(),
            scenario_set_version=scenario_set.version,
            scenario_set_hash=scenario_set.content_hash,
            scenario_ids=[scenario.scenario_id for scenario in scenarios],
            scenario_count=len(scenarios),
            groups=list(groups),
            random_seed=random_seed,
            model_provider=self._model.provider,
            model_name=self._model.model_name,
            model_is_mock=self._model.is_mock,
            structured_output=True,
            thinking_enabled=self._settings.model_enable_thinking,
            model_temperature=self._settings.model_temperature,
            model_timeout_seconds=self._settings.model_timeout_seconds,
            model_max_retries=self._settings.model_max_retries,
            prompt_version=PROMPT_VERSION,
            prompt_hash=prompt_hash(),
            reply_policy_version="deterministic-backend-v1",
            budget=budget,
            started_at=started_at,
            python_version=platform.python_version(),
        )

    @staticmethod
    def _git_state() -> tuple[str, bool]:
        project_root = Path(__file__).resolve().parents[2]
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            dirty = bool(
                subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=project_root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            return commit, dirty
        except (OSError, subprocess.CalledProcessError):
            return "unavailable", True

    @staticmethod
    def _application_version() -> str:
        try:
            return version("secondhand-agent-backend")
        except PackageNotFoundError:
            return "0.1.0"

    @staticmethod
    def _new_batch_id() -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"eval-{timestamp}-{uuid4().hex[:8]}"

    @staticmethod
    def _unexpected_failure(
        *,
        batch_id: str,
        scenario: EvaluationScenario,
        group: ExperimentGroup,
        random_seed: int,
        error_category: str,
    ) -> RunResult:
        now = datetime.now(UTC)
        run_id = f"failed-{uuid4()}"
        return RunResult(
            batch_id=batch_id,
            run_id=run_id,
            experiment_group=group,
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            random_seed=random_seed,
            started_at=now,
            completed_at=now,
            final_state=FinalState.SYSTEM_FAILURE,
            termination_reason="UNEXPECTED_EVALUATION_ERROR",
            violation=False,
            violation_codes=[],
            buyer_turn_count=0,
            formal_offer_round_count=0,
            formal_commitment_count=0,
            invalid_formal_commitment_count=0,
            approval_request_count=0,
            approval_approved_count=0,
            approval_rejected_count=0,
            approval_invalidated_count=0,
            model_call_count=0,
            usage_covered_call_count=0,
            input_tokens=None,
            output_tokens=None,
            cached_input_tokens=None,
            total_tokens=None,
            estimated_cost=None,
            cost_currency=None,
            error_category=error_category,
            commitments=[],
            events=[
                EvaluationEvent(
                    event_index=0,
                    event_type="RUN_STARTED",
                    action="START_SCENARIO",
                    outcome="STARTED",
                    occurred_at=now,
                    data={
                        "batch_id": batch_id,
                        "run_id": run_id,
                        "experiment_group": group.value,
                        "scenario_id": scenario.scenario_id,
                        "scenario_version": scenario.scenario_version,
                        "random_seed": random_seed,
                    },
                ),
                EvaluationEvent(
                    event_index=1,
                    event_type="RUN_COMPLETED",
                    action="CLASSIFY_RUN",
                    outcome="ERROR",
                    occurred_at=now,
                    data={
                        "final_state": FinalState.SYSTEM_FAILURE.value,
                        "termination_reason": "UNEXPECTED_EVALUATION_ERROR",
                        "error_category": error_category,
                    },
                ),
            ],
        )
