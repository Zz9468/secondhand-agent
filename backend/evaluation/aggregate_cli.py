import argparse
import json
from pathlib import Path

from evaluation.aggregate import aggregate_batches, write_aggregate_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聚合多个不可变真实模型评测批次。")
    parser.add_argument("batch_dirs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-mock", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = aggregate_batches(
        args.batch_dirs,
        require_clean=not args.allow_dirty,
        require_real_model=not args.allow_mock,
    )
    output = write_aggregate_report(result, args.output_dir)
    print(
        json.dumps(
            {
                "output_directory": str(output),
                "batch_count": len(result.manifests),
                "run_count": len(result.runs),
                "matched_scenario_count": len(result.matched_scenario_ids),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
