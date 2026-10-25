"""P4 real-model calls and native sanitized tracing on disposable databases."""

import argparse
import os
import sys
from pathlib import Path

from scripts.run_p0_tests import main as run_isolated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-model", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args, extra = parser.parse_known_args()
    target = args.output.resolve()
    if target.exists():
        parser.error("refusing to overwrite prior evidence")
    os.environ.update(RUN_P2_REAL_MODEL="1", P2_LIVE_REPORT=str(target), P4_TRACE="1")
    sys.argv = [sys.argv[0], "tests/live/test_p4_live_quality.py", *extra]
    return run_isolated()


if __name__ == "__main__":
    raise SystemExit(main())
