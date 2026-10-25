"""Replay saved native callbacks and publish frozen scores without invoking any model."""

import argparse
import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.p0.tracing import SafeTraceClient
from app.runtime.contracts import digest
from evaluation.p5.official import export_experiment
from evaluation.p5.runner import save_report, sha
from evaluation.p5.trace_journal import journal_summary, read_native_runs
from scripts.query_native_traces import query_native_traces


def validate_source(report_path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    journal = report_path.parent / "native-traces.jsonl"
    if not report.get("finished_at") or report.get("frozen_runtime", {}).get("status") != "PASS":
        raise ValueError("remote replay requires a completed, frozen source experiment")
    if sha(journal) != report.get("local_native_trace", {}).get("sha256"):
        raise ValueError("native journal hash mismatch")
    expected = {digest(r["run_id"]) for row in report["results"] for r in row.get("runs", [])}
    if journal_summary(journal, expected)["status"] != "PASS":
        raise ValueError("actual native callbacks are incomplete; reconstruction is forbidden")
    return report, read_native_runs(journal), expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-remote", action="store_true", required=True)
    args = parser.parse_args()
    report, spans, expected = validate_source(args.report)
    args.output.mkdir(parents=True, exist_ok=False)
    report["remote_replay"] = {
        "source_report_sha256": sha(args.report),
        "export_code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "model_calls": 0,
        "attempted_spans": 0,
        "uploaded_spans": 0,
        "note": "Original IDs, timestamps and callback fields retained; no reconstruction",
    }
    report["remote"] = []
    report["release_status"] = "INSUFFICIENT_EVIDENCE"
    settings = get_settings()
    if not settings.langsmith_api_key:
        report["remote_replay"]["error_type"] = "MissingLangSmithCredential"
        save_report(args.output, report)
        return 2
    client = SafeTraceClient(
        api_url=settings.langsmith_endpoint,
        api_key=settings.langsmith_api_key.get_secret_value(),
        auto_batch_tracing=False,
    )
    try:
        spans.sort(key=lambda r: (r.get("dotted_order", ""), r["id"]))
        for offset in range(0, len(spans), 100):
            batch = spans[offset : offset + 100]
            report["remote_replay"]["attempted_spans"] += len(batch)
            save_report(args.output, report)
            client.batch_ingest_runs(create=batch)
            report["remote_replay"]["uploaded_spans"] += len(batch)
        project = client.read_project(project_name="secondhand-agent-langgraph-test")
        native = asyncio.run(
            query_native_traces(
                client,
                project.id,
                datetime.fromisoformat(report["started_at"]),
                {"LangGraph", "parse_intents", "commit_business", "publish_reply"},
            )
        )
        refs = {(r.metadata or {}).get("run_ref") for r in native if r.end_time}
        report["native_trace"] = {
            "status": "PASS" if expected <= refs else "INSUFFICIENT_EVIDENCE",
            "expected_graph_runs": len(expected),
            "matched_graph_runs": len(expected & refs),
            "missing_refs": sorted(expected - refs),
            "spans": len(native),
            "origin": "Replayed actual sanitized native callbacks with original IDs/timestamps",
        }
        comparable = {c["case_id"] for c in report["cases"] if c["group"] == "business_preserving"}
        for label, rows, dataset in (
            ("graph", report["results"], "full"),
            (
                "graph-business",
                [r for r in report["results"] if r["case_id"] in comparable],
                "business",
            ),
            ("legacy-business", report["legacy_results"], "business"),
        ):
            result = export_experiment(rows, report["metadata"], label=label, dataset_label=dataset)
            result["target"] = label
            report["remote"].append(result)
            for row in rows:
                row["remote_returned"] = result["status"] == "PASS"
            save_report(args.output, report)
    except Exception as exc:
        report["remote_replay"].update(
            status="INSUFFICIENT_EVIDENCE", error_type=type(exc).__name__
        )
        save_report(args.output, report)
        return 2
    complete = len(report["remote"]) == 3 and all(r["status"] == "PASS" for r in report["remote"])
    complete = complete and report["native_trace"]["status"] == "PASS"
    report["remote_replay"]["status"] = "PASS" if complete else "INSUFFICIENT_EVIDENCE"
    # Full acceptance, including human risk labels, must still be evaluated separately.
    save_report(args.output, report)
    print(report["remote_replay"]["status"])
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
