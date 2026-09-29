import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from evaluation.gate import GateReport, evaluate_deterministic_gate
from evaluation.metrics import (
    DistributionMetric,
    EvaluationSummary,
    RatioMetric,
    summarize_runs,
)
from evaluation.schemas import BatchManifest, FinalState, RunResult

REPORT_GENERATOR_VERSION = "1.0.0"


def load_batch_records(
    batch_dir: Path,
) -> tuple[BatchManifest, list[RunResult], list[dict[str, object]]]:
    resolved = batch_dir.resolve()
    manifest = BatchManifest.model_validate_json(
        (resolved / "manifest.json").read_bytes()
    )
    runs: list[RunResult] = []
    for line in (resolved / "runs.jsonl").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        payload["events"] = []
        run = RunResult.model_validate(payload)
        if run.batch_id != manifest.batch_id:
            raise ValueError("运行明细 batch_id 与清单不一致")
        runs.append(run)
    event_rows = [
        json.loads(line)
        for line in (resolved / "events.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    if len(runs) != manifest.started_run_count:
        raise ValueError("运行明细数量与清单 started_run_count 不一致")
    return manifest, runs, event_rows


def write_derived_artifacts(batch_dir: Path) -> GateReport:
    """只从已落盘 JSONL 重算派生产物，使报告可独立复核。"""

    resolved = batch_dir.resolve()
    manifest, runs, event_rows = load_batch_records(resolved)
    summary = summarize_runs(runs)
    gate = evaluate_deterministic_gate(manifest, runs, event_rows)

    _write_json(resolved / "summary.json", summary.model_dump(mode="json"))
    _write_json(resolved / "gate.json", gate.model_dump(mode="json"))
    _write_metrics_csv(resolved / "metrics.csv", summary)
    _write_runs_csv(resolved / "runs.csv", runs)
    _write_manual_review_csv(resolved / "manual_review.csv", runs)

    source_hashes = {
        name: _sha256(resolved / name)
        for name in ("manifest.json", "runs.jsonl", "events.jsonl")
    }
    report_manifest = {
        "report_generator_version": REPORT_GENERATOR_VERSION,
        "batch_id": manifest.batch_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_sha256": source_hashes,
        "summary_schema_version": summary.result_schema_version,
        "gate_status": gate.status.value,
    }
    _write_json(resolved / "report_manifest.json", report_manifest)
    (resolved / "report.md").write_text(
        _markdown_report(
            manifest=manifest,
            runs=runs,
            summary=summary,
            gate=gate,
            source_hashes=source_hashes,
        ),
        encoding="utf-8",
    )
    return gate


def _write_metrics_csv(path: Path, summary: EvaluationSummary) -> None:
    fieldnames = [
        "experiment_group",
        "started_run_count",
        "completed_run_count",
        "intent_agreement_numerator",
        "intent_agreement_denominator",
        "intent_agreement_rate",
        "valid_termination_numerator",
        "valid_termination_denominator",
        "valid_termination_rate",
        "violating_run_numerator",
        "violating_run_denominator",
        "violating_run_rate",
        "invalid_commitment_numerator",
        "invalid_commitment_denominator",
        "invalid_formal_commitment_rate",
        "average_negotiation_turns",
        "approval_run_rate",
        "model_call_count",
        "usage_coverage_rate",
        "total_tokens",
        "estimated_cost_by_currency",
    ]
    rows: list[dict[str, object]] = []
    for item in summary.groups:
        rows.append(
            {
                "experiment_group": item.experiment_group.value,
                "started_run_count": item.started_run_count,
                "completed_run_count": item.completed_run_count,
                **_ratio_columns("intent_agreement", item.intent_agreement_rate),
                **_ratio_columns("valid_termination", item.valid_termination_rate),
                **_ratio_columns("violating_run", item.violating_run_rate),
                **_ratio_columns(
                    "invalid_commitment",
                    item.invalid_formal_commitment_rate,
                    value_name="invalid_formal_commitment_rate",
                ),
                "average_negotiation_turns": item.average_negotiation_turns,
                "approval_run_rate": item.approval_run_rate.value,
                "model_call_count": item.model_call_count,
                "usage_coverage_rate": item.usage_coverage_rate.value,
                "total_tokens": item.total_tokens,
                "estimated_cost_by_currency": json.dumps(
                    item.estimated_cost_by_currency,
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                ),
            }
        )
    _write_csv(path, fieldnames, rows)


def _ratio_columns(
    prefix: str,
    metric: RatioMetric,
    *,
    value_name: str | None = None,
) -> dict[str, object]:
    return {
        f"{prefix}_numerator": metric.numerator,
        f"{prefix}_denominator": metric.denominator,
        value_name or f"{prefix}_rate": metric.value,
    }


def _write_runs_csv(path: Path, runs: list[RunResult]) -> None:
    fieldnames = [
        "batch_id",
        "run_id",
        "experiment_group",
        "scenario_id",
        "scenario_version",
        "final_state",
        "termination_reason",
        "violation",
        "violation_codes",
        "buyer_turn_count",
        "formal_offer_round_count",
        "formal_commitment_count",
        "invalid_formal_commitment_count",
        "approval_request_count",
        "model_call_count",
        "usage_covered_call_count",
        "total_tokens",
        "estimated_cost",
        "cost_currency",
        "error_category",
    ]
    rows = [
        {
            "batch_id": run.batch_id,
            "run_id": run.run_id,
            "experiment_group": run.experiment_group.value,
            "scenario_id": run.scenario_id,
            "scenario_version": run.scenario_version,
            "final_state": run.final_state.value,
            "termination_reason": run.termination_reason,
            "violation": run.violation,
            "violation_codes": ";".join(run.violation_codes),
            "buyer_turn_count": run.buyer_turn_count,
            "formal_offer_round_count": run.formal_offer_round_count,
            "formal_commitment_count": run.formal_commitment_count,
            "invalid_formal_commitment_count": run.invalid_formal_commitment_count,
            "approval_request_count": run.approval_request_count,
            "model_call_count": run.model_call_count,
            "usage_covered_call_count": run.usage_covered_call_count,
            "total_tokens": run.total_tokens,
            "estimated_cost": run.estimated_cost,
            "cost_currency": run.cost_currency,
            "error_category": run.error_category,
        }
        for run in runs
    ]
    _write_csv(path, fieldnames, rows)


def _write_manual_review_csv(path: Path, runs: list[RunResult]) -> None:
    fieldnames = [
        "run_id",
        "experiment_group",
        "scenario_id",
        "final_state",
        "review_priority",
        "rule_flags",
        "goal_attainment_1_to_5",
        "response_relevance_1_to_5",
        "naturalness_1_to_5",
        "unnecessary_concession_yes_no",
        "reviewer",
        "review_notes",
    ]
    preserved = _load_manual_reviews(path)
    human_fields = (
        "goal_attainment_1_to_5",
        "response_relevance_1_to_5",
        "naturalness_1_to_5",
        "unnecessary_concession_yes_no",
        "reviewer",
        "review_notes",
    )
    rows: list[dict[str, object]] = []
    for run in runs:
        flags: list[str] = []
        if run.final_state is FinalState.SYSTEM_FAILURE:
            flags.append("SYSTEM_FAILURE")
        if run.final_state is FinalState.UNRESOLVED:
            flags.append("UNRESOLVED")
        if run.violation:
            flags.append("DETERMINISTIC_VIOLATION")
        if run.approval_request_count:
            flags.append("HUMAN_APPROVAL_USED")
        if run.buyer_turn_count >= 4:
            flags.append("LONG_NEGOTIATION")
        priority = (
            "HIGH"
            if {"SYSTEM_FAILURE", "DETERMINISTIC_VIOLATION"} & set(flags)
            else "MEDIUM"
            if flags
            else "STANDARD"
        )
        row: dict[str, object] = {
                "run_id": run.run_id,
                "experiment_group": run.experiment_group.value,
                "scenario_id": run.scenario_id,
                "final_state": run.final_state.value,
                "review_priority": priority,
                "rule_flags": ";".join(flags),
                "goal_attainment_1_to_5": "",
                "response_relevance_1_to_5": "",
                "naturalness_1_to_5": "",
                "unnecessary_concession_yes_no": "",
                "reviewer": "",
                "review_notes": "",
        }
        previous = preserved.get(run.run_id, {})
        for field in human_fields:
            row[field] = previous.get(field, "")
        rows.append(row)
    _write_csv(path, fieldnames, rows)


def _load_manual_reviews(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["run_id"]: row
            for row in csv.DictReader(handle)
            if row.get("run_id")
        }


def _write_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, object]],
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _markdown_report(
    *,
    manifest: BatchManifest,
    runs: list[RunResult],
    summary: EvaluationSummary,
    gate: GateReport,
    source_hashes: dict[str, str],
) -> str:
    lines = [
        f"# SecondHand Agent 离线评测报告：{manifest.batch_id}",
        "",
        "> 本报告由机器可读 JSONL 自动生成。AGREED 仅表示交易意向，"
        "不表示支付、库存锁定、订单或履约完成。",
        "",
        "## 运行元数据",
        "",
        f"- Git 提交：`{manifest.git_commit}`",
        f"- 工作区状态：`{'dirty' if manifest.git_worktree_dirty else 'clean'}`",
        f"- 场景集：`{manifest.scenario_set_version}` / `{manifest.scenario_set_hash}`",
        f"- 模型：`{manifest.model_provider}/{manifest.model_name}`；"
        f"模拟模型：`{str(manifest.model_is_mock).lower()}`",
        f"- Prompt：`{manifest.prompt_version}` / `{manifest.prompt_hash}`",
        f"- 随机种子：`{manifest.random_seed}`",
        f"- 样本：`{manifest.started_run_count}`；系统失败："
        f"`{manifest.failed_run_count}`",
        f"- 时间：`{manifest.started_at.isoformat()}` → "
        f"`{manifest.completed_at.isoformat() if manifest.completed_at else 'unknown'}`",
        f"- 确定性安全门禁：`{gate.status.value}`",
        "",
        "## 分组指标",
        "",
        "| 组 | 已开始/完成 | 意向达成率 | 有效结束率 | 运行违规率 | "
        "正式承诺违规率 | 平均轮次 | 审批率 | 用量覆盖率 | Token |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
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
                    _format_ratio(item.violating_run_rate),
                    _format_ratio(item.invalid_formal_commitment_rate),
                    _format_number(item.average_negotiation_turns),
                    _format_ratio(item.approval_run_rate),
                    _format_ratio(item.usage_coverage_rate),
                    str(item.total_tokens) if item.total_tokens is not None else "未知",
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 完成样本分布",
            "",
            "完成样本指执行终态不是 `SYSTEM_FAILURE` 的运行。P95 使用 nearest-rank "
            "方法；Token 或费用缺失时只统计有提供商用量的样本，并由覆盖率揭示缺口。",
            "",
            "| 组 | 模型调用/样本（均值/中位/P95） | "
            "Token/样本（均值/中位/P95） | 费用/样本 |",
            "|---|---:|---:|---|",
        ]
    )
    for item in summary.groups:
        costs = ", ".join(
            f"{currency}: {_format_distribution(distribution)}"
            for currency, distribution in (
                item.estimated_cost_per_completed_run_by_currency.items()
            )
        ) or "未知"
        lines.append(
            f"| {item.experiment_group.value} | "
            f"{_format_distribution(item.model_calls_per_completed_run)} | "
            f"{_format_distribution(item.total_tokens_per_completed_run)} | "
            f"{costs} |"
        )

    failures = Counter(
        run.error_category or run.termination_reason
        for run in runs
        if run.final_state is FinalState.SYSTEM_FAILURE
    )
    lines.extend(["", "## 系统失败", ""])
    if failures:
        for category, count in sorted(failures.items()):
            lines.append(f"- `{category}`：{count}")
    else:
        lines.append("- 无。")

    lines.extend(["", "## 确定性安全门禁", ""])
    for check in gate.checks:
        marker = "PASS" if check.passed else "FAIL"
        lines.append(f"- `{marker}` `{check.check_id}`：{check.detail}")

    lines.extend(
        [
            "",
            "## 人工复核边界",
            "",
            "自然度、相关性、目标达成质量和是否存在不必要让步不是确定性安全事实，"
            "不会进入自动门禁。`manual_review.csv` 按预定义规则给出优先级，"
            "保留 1–5 分、是否不必要让步、复核人和备注空栏，由人工填写。",
            "",
            "## 原始证据",
            "",
            f"- `manifest.json` SHA-256：`{source_hashes['manifest.json']}`",
            f"- `runs.jsonl` SHA-256：`{source_hashes['runs.jsonl']}`",
            f"- `events.jsonl` SHA-256：`{source_hashes['events.jsonl']}`",
            "- 机器可读派生产物：`summary.json`、`metrics.csv`、`runs.csv`、"
            "`gate.json`、`report_manifest.json`。",
            "",
        ]
    )
    return "\n".join(lines)


def _format_ratio(metric: RatioMetric) -> str:
    if metric.value is None:
        return f"{metric.numerator}/{metric.denominator} (null)"
    return f"{metric.numerator}/{metric.denominator} ({metric.value:.2%})"


def _format_number(value: float | None) -> str:
    return "null" if value is None else f"{value:.3f}"


def _format_distribution(metric: DistributionMetric) -> str:
    if metric.count == 0:
        return "null"
    return (
        f"{_format_number(metric.mean)}/"
        f"{_format_number(metric.median)}/"
        f"{_format_number(metric.p95)} (n={metric.count})"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
