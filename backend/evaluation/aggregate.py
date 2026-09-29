import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evaluation.metrics import (
    DistributionMetric,
    EvaluationSummary,
    RatioMetric,
    summarize_runs,
)
from evaluation.reporting import load_batch_records
from evaluation.schemas import BatchManifest, ExperimentGroup, FinalState, RunResult

AGGREGATE_REPORT_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class AggregateResult:
    manifests: tuple[BatchManifest, ...]
    runs: tuple[RunResult, ...]
    summary: EvaluationSummary
    matched_summary: EvaluationSummary
    matched_scenario_ids: tuple[str, ...]
    source_hashes: dict[str, dict[str, str]]


def aggregate_batches(
    batch_dirs: list[Path],
    *,
    require_clean: bool = True,
    require_real_model: bool = True,
) -> AggregateResult:
    if not batch_dirs:
        raise ValueError("至少需要一个评测批次")

    manifests: list[BatchManifest] = []
    runs: list[RunResult] = []
    source_hashes: dict[str, dict[str, str]] = {}
    seen_batch_ids: set[str] = set()
    for batch_dir in batch_dirs:
        resolved = batch_dir.resolve()
        manifest, batch_runs, _ = load_batch_records(resolved)
        if manifest.batch_id in seen_batch_ids:
            raise ValueError(f"批次重复: {manifest.batch_id}")
        seen_batch_ids.add(manifest.batch_id)
        manifests.append(manifest)
        runs.extend(batch_runs)
        source_hashes[manifest.batch_id] = {
            name: _sha256(resolved / name)
            for name in ("manifest.json", "runs.jsonl", "events.jsonl")
        }

    _validate_compatible(
        manifests,
        runs,
        require_clean=require_clean,
        require_real_model=require_real_model,
    )
    scenario_ids_by_group: dict[ExperimentGroup, set[str]] = defaultdict(set)
    for run in runs:
        scenario_ids_by_group[run.experiment_group].add(run.scenario_id)
    comparable_groups = {
        ExperimentGroup.PROMPT_ONLY,
        ExperimentGroup.FULL_WORKFLOW,
    }
    if comparable_groups.issubset(scenario_ids_by_group):
        matched_ids = tuple(
            sorted(
                scenario_ids_by_group[ExperimentGroup.PROMPT_ONLY]
                & scenario_ids_by_group[ExperimentGroup.FULL_WORKFLOW]
            )
        )
    else:
        matched_ids = ()
    matched_runs = [
        run
        for run in runs
        if run.scenario_id in matched_ids
        and run.experiment_group in comparable_groups
    ]
    return AggregateResult(
        manifests=tuple(manifests),
        runs=tuple(runs),
        summary=summarize_runs(runs),
        matched_summary=summarize_runs(matched_runs),
        matched_scenario_ids=matched_ids,
        source_hashes=source_hashes,
    )


def write_aggregate_report(result: AggregateResult, output_dir: Path) -> Path:
    resolved = output_dir.resolve()
    resolved.mkdir(parents=True, exist_ok=False)
    manifest = {
        "aggregate_report_version": AGGREGATE_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": result.manifests[0].git_commit,
        "scenario_set_version": result.manifests[0].scenario_set_version,
        "scenario_set_hash": result.manifests[0].scenario_set_hash,
        "model_provider": result.manifests[0].model_provider,
        "model_name": result.manifests[0].model_name,
        "model_input_price_per_million": (
            result.manifests[0].model_input_price_per_million
        ),
        "model_output_price_per_million": (
            result.manifests[0].model_output_price_per_million
        ),
        "model_cached_input_price_per_million": (
            result.manifests[0].model_cached_input_price_per_million
        ),
        "model_cost_currency": result.manifests[0].model_cost_currency,
        "prompt_version": result.manifests[0].prompt_version,
        "prompt_hash": result.manifests[0].prompt_hash,
        "batch_ids": [item.batch_id for item in result.manifests],
        "random_seeds": [item.random_seed for item in result.manifests],
        "random_seeds_by_group": {
            group.value: sorted(
                {
                    manifest.random_seed
                    for manifest in result.manifests
                    if group in manifest.groups
                }
            )
            for group in ExperimentGroup
            if any(group in manifest.groups for manifest in result.manifests)
        },
        "source_batch_count": len(result.manifests),
        "run_count": len(result.runs),
        "matched_scenario_ids": list(result.matched_scenario_ids),
        "source_sha256": result.source_hashes,
    }
    _write_json(resolved / "aggregate_manifest.json", manifest)
    _write_json(resolved / "summary.json", result.summary.model_dump(mode="json"))
    _write_json(
        resolved / "matched_summary.json",
        result.matched_summary.model_dump(mode="json"),
    )
    _write_jsonl(
        resolved / "runs.jsonl",
        [run.model_dump(mode="json", exclude={"events"}) for run in result.runs],
    )
    (resolved / "report.md").write_text(
        _markdown_report(result),
        encoding="utf-8",
    )
    return resolved


def _validate_compatible(
    manifests: list[BatchManifest],
    runs: list[RunResult],
    *,
    require_clean: bool,
    require_real_model: bool,
) -> None:
    reference = manifests[0]
    compatibility_fields = (
        "contract_version",
        "result_schema_version",
        "git_commit",
        "application_version",
        "python_version",
        "database_type",
        "migration_version",
        "scenario_set_version",
        "scenario_set_hash",
        "model_provider",
        "model_name",
        "model_is_mock",
        "structured_output",
        "thinking_enabled",
        "model_temperature",
        "model_timeout_seconds",
        "model_max_retries",
        "model_input_price_per_million",
        "model_output_price_per_million",
        "model_cached_input_price_per_million",
        "model_cost_currency",
        "prompt_version",
        "prompt_hash",
        "reply_policy_version",
    )
    for manifest in manifests[1:]:
        for field in compatibility_fields:
            if getattr(manifest, field) != getattr(reference, field):
                raise ValueError(f"批次元数据不兼容: {field}")
    if require_clean and any(item.git_worktree_dirty for item in manifests):
        raise ValueError("正式聚合报告要求所有批次来自干净工作区")
    if require_real_model and any(item.model_is_mock for item in manifests):
        raise ValueError("正式聚合报告要求所有批次使用真实模型")
    if len({run.run_id for run in runs}) != len(runs):
        raise ValueError("聚合运行存在重复 run_id")

    runs_by_batch: dict[str, list[RunResult]] = defaultdict(list)
    for run in runs:
        runs_by_batch[run.batch_id].append(run)

    group_scenario_sets: dict[ExperimentGroup, set[str]] = {}
    seeds_by_group: dict[ExperimentGroup, set[int]] = defaultdict(set)
    observed_group_seeds: set[tuple[ExperimentGroup, int]] = set()
    for manifest in manifests:
        if manifest.completed_at is None:
            raise ValueError(f"批次未完成: {manifest.batch_id}")
        if manifest.scenario_count != len(manifest.scenario_ids):
            raise ValueError(f"批次场景计数不一致: {manifest.batch_id}")
        if len(set(manifest.scenario_ids)) != len(manifest.scenario_ids):
            raise ValueError(f"批次场景 ID 重复: {manifest.batch_id}")
        if len(set(manifest.groups)) != len(manifest.groups):
            raise ValueError(f"批次实验组重复: {manifest.batch_id}")

        batch_runs = runs_by_batch.get(manifest.batch_id, [])
        expected_pairs = {
            (scenario_id, group)
            for scenario_id in manifest.scenario_ids
            for group in manifest.groups
        }
        observed_pairs = [
            (run.scenario_id, run.experiment_group) for run in batch_runs
        ]
        if Counter(observed_pairs) != Counter(expected_pairs):
            raise ValueError(f"批次运行矩阵不完整或重复: {manifest.batch_id}")
        if manifest.started_run_count != len(batch_runs):
            raise ValueError(f"批次运行计数不一致: {manifest.batch_id}")
        failed_count = sum(
            run.final_state is FinalState.SYSTEM_FAILURE for run in batch_runs
        )
        if manifest.failed_run_count != failed_count:
            raise ValueError(f"批次失败计数不一致: {manifest.batch_id}")

        scenario_ids = set(manifest.scenario_ids)
        for group in manifest.groups:
            group_seed = (group, manifest.random_seed)
            if group_seed in observed_group_seeds:
                raise ValueError(
                    f"{group.value} 组聚合批次必须使用不同随机种子"
                )
            observed_group_seeds.add(group_seed)
            seeds_by_group[group].add(manifest.random_seed)
            expected_ids = group_scenario_sets.setdefault(group, scenario_ids)
            if expected_ids != scenario_ids:
                raise ValueError(f"{group.value} 组各重复批次的场景集合不一致")

    prompt_group = ExperimentGroup.PROMPT_ONLY
    workflow_group = ExperimentGroup.FULL_WORKFLOW
    if prompt_group in seeds_by_group and workflow_group in seeds_by_group:
        if seeds_by_group[prompt_group] != seeds_by_group[workflow_group]:
            raise ValueError("A/C 匹配对比必须使用相同的随机种子集合")
        if not group_scenario_sets[prompt_group].issubset(
            group_scenario_sets[workflow_group]
        ):
            raise ValueError("A 组高风险场景必须是 C 组全量场景的子集")


def _markdown_report(result: AggregateResult) -> str:
    reference = result.manifests[0]
    lines = [
        "# SecondHand Agent 最终真实模型效果评测",
        "",
        "> 本报告只聚合原始批次，不删除失败样本。AGREED 仅表示交易意向，"
        "不表示支付、库存锁定、订单或履约完成。",
        "",
        "## 可追溯元数据",
        "",
        f"- Git 提交：`{reference.git_commit}`（所有批次工作区均为 clean）",
        f"- 场景集：`{reference.scenario_set_version}` / `{reference.scenario_set_hash}`",
        f"- 模型：`{reference.model_provider}/{reference.model_name}`",
        "- 价格快照（每百万 Token）：输入 "
        f"`{reference.model_input_price_per_million}`；输出 "
        f"`{reference.model_output_price_per_million}`；缓存输入 "
        f"`{reference.model_cached_input_price_per_million}`；币种 "
        f"`{reference.model_cost_currency or '未配置'}`",
        f"- Prompt：`{reference.prompt_version}` / `{reference.prompt_hash}`",
        f"- 批次：{', '.join(f'`{item.batch_id}`' for item in result.manifests)}",
        f"- 随机种子：{', '.join(str(item.random_seed) for item in result.manifests)}",
        f"- 总运行数：{len(result.runs)}",
        "",
        "## 全量指标",
        "",
        *_summary_table(result.summary),
    ]
    if result.matched_scenario_ids:
        lines.extend(
            [
                "",
                "## A/C 匹配高风险子集",
                "",
                f"匹配场景数：{len(result.matched_scenario_ids)}。只在相同场景和重复次数"
                "上比较 A 与 C，避免不同场景构成造成假提升。",
                "",
                *_summary_table(result.matched_summary),
            ]
        )
    lines.extend(["", "## 原始批次哈希", ""])
    for batch_id, hashes in result.source_hashes.items():
        lines.append(
            f"- `{batch_id}`：manifest `{hashes['manifest.json']}`；"
            f"runs `{hashes['runs.jsonl']}`；events `{hashes['events.jsonl']}`"
        )
    lines.append("")
    return "\n".join(lines)


def _summary_table(summary: EvaluationSummary) -> list[str]:
    lines = [
        "| 组 | 运行/完成 | 意向达成 | 有效结束 | 系统失败 | 运行违规 | 无效正式承诺 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.groups:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.experiment_group.value,
                    f"{item.started_run_count}/{item.completed_run_count}",
                    _format_ratio(item.intent_agreement_rate),
                    _format_ratio(item.valid_termination_rate),
                    _format_ratio(item.system_failure_rate),
                    _format_ratio(item.violating_run_rate),
                    _format_ratio(item.invalid_formal_commitment_rate),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| 组 | 自动接受路由 | 审批路由 | 禁止报价阻断 | "
            "未授权条件阻断 | 不可计算条件阻断 | 审批解决 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in summary.groups:
        lines.append(
            "| "
            + " | ".join(
                [
                    item.experiment_group.value,
                    _format_ratio(item.auto_accept_routing_accuracy),
                    _format_ratio(item.approval_routing_accuracy),
                    _format_ratio(item.prohibited_offer_block_rate),
                    _format_ratio(item.unsupported_terms_block_rate),
                    _format_ratio(item.invalid_offer_terms_block_rate),
                    _format_ratio(item.approval_resolution_rate),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| 组 | 模型决策成功 | 可用结构化响应/尝试 | 用量覆盖 | "
            "模型尝试 | Token | 估算费用 | 模型耗时 ms（均值/中位/P95） | "
            "失败调用/Token |",
            "|---|---:|---:|---:|---:|---:|---|---|---:|",
        ]
    )
    for item in summary.groups:
        costs = ", ".join(
            f"{currency} {amount}"
            for currency, amount in sorted(item.estimated_cost_by_currency.items())
        ) or "未知"
        lines.append(
            "| "
            + " | ".join(
                [
                    item.experiment_group.value,
                    _format_ratio(item.model_decision_success_rate),
                    _format_ratio(item.model_call_success_rate),
                    _format_ratio(item.usage_coverage_rate),
                    str(item.model_call_count),
                    str(item.total_tokens) if item.total_tokens is not None else "未知",
                    costs,
                    _format_distribution(item.model_duration_ms_per_completed_run),
                    f"{item.system_failure_model_call_count}/"
                    + (
                        str(item.system_failure_total_tokens)
                        if item.system_failure_total_tokens is not None
                        else "未知"
                    ),
                ]
            )
            + " |"
        )
    return lines


def _format_ratio(metric: RatioMetric) -> str:
    if metric.value is None:
        return f"{metric.numerator}/{metric.denominator} (null)"
    interval = ""
    if metric.ci95_lower is not None and metric.ci95_upper is not None:
        interval = f"; CI {metric.ci95_lower:.1%}–{metric.ci95_upper:.1%}"
    return f"{metric.numerator}/{metric.denominator} ({metric.value:.1%}{interval})"


def _format_distribution(metric: DistributionMetric) -> str:
    if metric.count == 0:
        return "null"
    return (
        f"{metric.mean:.3f}/"
        f"{metric.median:.3f}/"
        f"{metric.p95:.3f} (n={metric.count})"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
