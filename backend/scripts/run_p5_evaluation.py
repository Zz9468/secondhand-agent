"""P5 opt-in real-model experiments on disposable MySQL/PostgreSQL databases."""

import argparse
import json
import os
import sys
from pathlib import Path

from scripts.run_p0_tests import main as run_isolated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-model", required=True, action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--select", nargs="+")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument(
        "--trace-local",
        action="store_true",
        help="Record sanitized native callbacks locally even when remote quota is unavailable",
    )
    args = parser.parse_args()
    if args.output.exists() or args.repetitions < 1 or not 1 <= args.concurrency <= 8:
        parser.error("use a new output directory, positive repetitions and concurrency 1..8")
    options = {**vars(args), "output": str(args.output.resolve())}
    os.environ["P5_EVALUATION_OPTIONS"] = json.dumps(options)
    os.environ["GRAPH_LANGSMITH_TRACING"] = "false"
    sys.argv = [sys.argv[0], "tests/live/test_p5_experiment.py", "-s"]
    return run_isolated()


if __name__ == "__main__":
    raise SystemExit(main())
