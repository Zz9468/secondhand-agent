import argparse
import json
from pathlib import Path

from evaluation.gate import GateStatus
from evaluation.reporting import write_derived_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从已有 JSONL 原始记录重新生成报告并执行确定性安全门禁。"
    )
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument(
        "--no-enforce-gate",
        action="store_true",
        help="仍生成 gate.json，但门禁失败时返回成功退出码。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gate = write_derived_artifacts(args.batch_dir)
    print(
        json.dumps(
            {
                "batch_id": gate.batch_id,
                "gate_status": gate.status.value,
                "report": str(args.batch_dir.resolve() / "report.md"),
            },
            ensure_ascii=False,
        )
    )
    if gate.status is GateStatus.FAIL and not args.no_enforce_gate:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
