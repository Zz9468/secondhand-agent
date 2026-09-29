import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.agent.model_factory import QwenChatModelFactory
from app.core.config import get_settings
from evaluation.artifacts import write_batch_artifacts
from evaluation.gate import GateReport, GateStatus
from evaluation.models import LangChainEvaluationModel, ScriptedEvaluationModel
from evaluation.runner import EvaluationRunner
from evaluation.scenarios import default_scenario_path, load_scenarios
from evaluation.schemas import EvaluationBudget, ExperimentGroup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行不接触业务数据库的 V3 阶段七离线 A/B/C 对比实验。"
    )
    parser.add_argument("--scenario-file", type=Path, default=default_scenario_path())
    parser.add_argument("--scenario-id", action="append", dest="scenario_ids")
    parser.add_argument(
        "--groups",
        nargs="+",
        choices=[group.value for group in ExperimentGroup],
        default=[group.value for group in ExperimentGroup],
    )
    parser.add_argument("--model", choices=["scripted", "qwen"], default="scripted")
    parser.add_argument(
        "--allow-real-model",
        action="store_true",
        help="显式允许访问付费真实模型；普通运行和测试不会启用。",
    )
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--batch-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-samples", type=int, default=24)
    parser.add_argument("--max-model-calls", type=int, default=100)
    parser.add_argument("--max-tokens", type=int, default=200000)
    parser.add_argument("--max-cost", type=Decimal)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--no-enforce-gate",
        action="store_true",
        help="仍生成门禁结果，但确定性门禁失败时返回成功退出码。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.model == "qwen":
        if not args.allow_real_model:
            raise SystemExit("真实模型必须显式传入 --allow-real-model")
        if not settings.model_is_configured:
            raise SystemExit("真实模型配置不完整")
        if args.max_cost is None:
            raise SystemExit("真实模型必须显式传入 --max-cost 费用上限")
        if not settings.model_cost_is_configured:
            raise SystemExit("真实模型费用预算要求配置模型单价与币种")
        # 评测禁用 SDK 隐式重试，使模型调用数与预算、原始事件一一对应。
        effective_settings = settings.model_copy(update={"model_max_retries": 0})
        model = LangChainEvaluationModel(
            QwenChatModelFactory().create(effective_settings)
        )
    else:
        effective_settings = settings
        model = ScriptedEvaluationModel()

    budget = EvaluationBudget(
        max_samples=args.max_samples,
        max_model_calls=args.max_model_calls,
        max_tokens=args.max_tokens,
        max_estimated_cost=args.max_cost,
        timeout_seconds=args.timeout_seconds,
    )
    result = EvaluationRunner(model=model, settings=effective_settings).run(
        scenario_set=load_scenarios(args.scenario_file),
        groups=tuple(ExperimentGroup(value) for value in args.groups),
        random_seed=args.seed,
        budget=budget,
        batch_id=args.batch_id,
        scenario_ids=set(args.scenario_ids) if args.scenario_ids else None,
    )
    batch_dir = write_batch_artifacts(result, args.output_dir)
    gate = GateReport.model_validate_json((batch_dir / "gate.json").read_bytes())
    print(
        json.dumps(
            {
                "batch_id": result.manifest.batch_id,
                "result_directory": str(batch_dir),
                "started_run_count": result.manifest.started_run_count,
                "failed_run_count": result.manifest.failed_run_count,
                "model_is_mock": result.manifest.model_is_mock,
                "gate_status": gate.status.value,
                "report": str(batch_dir / "report.md"),
            },
            ensure_ascii=False,
        )
    )
    if gate.status is GateStatus.FAIL and not args.no_enforce_gate:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
