"""Export synthetic native traces then read them back to verify actual remote storage."""

import argparse
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from langsmith.utils import LangSmithNotFoundError

from app.p0.tracing import trace_client
from tests.p0.test_tracing import MARKER, exercise_native_traces

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("refusing to overwrite an existing probe")
    config = dotenv_values(ROOT / ".env")
    logging.getLogger("langsmith").setLevel(logging.CRITICAL)
    report = {"project": "secondhand-agent-langgraph-test", "passed": False}
    try:
        client = trace_client(
            api_url=config["LANGSMITH_ENDPOINT"], api_key=config["LANGSMITH_API_KEY"]
        )
        try:
            project = client.read_project(project_name=report["project"])
        except LangSmithNotFoundError:
            project = client.create_project(report["project"])
        start = datetime.now(UTC)
        exercise_native_traces(client, report["project"])
        deadline = time.monotonic() + 20
        while True:
            runs = list(client.list_runs(project_id=project.id, start_time=start, limit=30))
            names = {run.name for run in runs}
            if {"p0_trace", "query_product", "p0_error", "p0_attachment"} <= names:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("remote trace visibility timeout")
            time.sleep(1)
        assert any(run.run_type == "llm" for run in runs)
        for run in runs:
            raw = run.model_dump_json()
            assert MARKER not in raw
            assert not run.attachments
        report.update(
            passed=True,
            run_count=len(runs),
            names=sorted(names),
            run_ids=[str(run.id) for run in runs],
            example_url=client.get_run_url(run=runs[0]),
        )
    except Exception as error:
        report["error_type"] = type(error).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
