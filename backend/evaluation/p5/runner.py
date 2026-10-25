"""Reproducible P5 experiment runner. Results are saved before remote publication."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.p0.tracing import trace_client
from app.runtime.contracts import digest
from app.semantic.model import PROMPTS, SemanticModel
from app.semantic.quality import PROMPTS as QUALITY_PROMPTS
from evaluation.p5.datasets import load_cases
from evaluation.p5.official import BUYER_PROMPT, JUDGE_PROMPT, export_experiment, judge, simulate
from evaluation.p5.scoring import acceptance, percentile, risk_report
from evaluation.p5.target import GraphTarget, score_case
from scripts.query_native_traces import query_native_traces
from scripts.run_p0_tests import ROOT, mysql_admin

BASELINE = "9fa17569010763860b45d541ce4e2dde46bb7b55"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(cases):
    settings = get_settings()
    return {
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        ),
        "graph_version": "negotiation-v3",
        "baseline_commit": BASELINE,
        "dataset_sha256": digest(cases),
        "prompt_sha256": digest([PROMPTS, QUALITY_PROMPTS]),
        "evaluator_prompt_sha256": digest([BUYER_PROMPT, JUDGE_PROMPT]),
        "dependency_lock_sha256": sha(ROOT / "backend/requirements.lock"),
        "packages": {
            p: importlib.metadata.version(p)
            for p in ("langgraph", "langsmith", "openevals", "agentevals", "langchain-openai")
        },
        "model": settings.model_name,
        "temperature": 0,
        "enable_thinking": False,
        "sdk_retries": 0,
        "experiment_spend_cap": None,
        "runtime_budget": {"calls": 10, "tokens": 180000, "active_seconds": 120},
        "cost_rates_per_million": [str(x) for x in SemanticModel().prices()]
        if SemanticModel().prices()
        else None,
    }


def summarize(rows):
    times = [v for row in rows for v in row.get("latency_seconds", [])]
    calls = [c for row in rows for run in row.get("runs", []) for c in run.get("calls", [])]
    calls += [c for row in rows for c in row.get("recorded_calls", [])]
    auxiliary = [c for row in rows for c in row.get("auxiliary_calls", [])]
    known = [
        Decimal(c["estimated_cny"]) for c in calls + auxiliary if c.get("estimated_cny") is not None
    ]
    runs = [r for row in rows for r in row.get("runs", [])]
    return {
        "runs": len(rows),
        "execution_errors": sum(r["status"] != "OK" for r in rows),
        "business_passes": sum(r.get("business_pass") is True for r in rows),
        "p50_seconds": percentile(times, 0.5),
        "p95_seconds": percentile(times, 0.95),
        "seller_calls": len(calls),
        "auxiliary_calls": len(auxiliary),
        "tokens": sum(c.get("tokens") or 0 for c in calls + auxiliary),
        "unknown_usage_calls": sum(c.get("tokens") is None for c in calls + auxiliary),
        "known_estimated_cny": str(sum(known, Decimal(0))),
        "unknown_cost_calls": sum(c.get("estimated_cny") is None for c in calls + auxiliary),
        "fallback_turns": sum(r.get("fallback") is True for r in runs),
        "seller_turns": len(runs),
        "judge_passes": sum(r.get("judge", {}).get("score") is True for r in rows),
        "judge_evaluated": sum(type(r.get("judge", {}).get("score")) is bool for r in rows),
    }


def run_legacy(cases, repetitions, directory, concurrency=1):
    checkout = directory / "legacy-source"
    archive = directory / "legacy-source.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", f"--output={archive}", BASELINE], check=True, cwd=ROOT
    )
    checkout.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (checkout / member.filename).resolve()
            if not target.is_relative_to(checkout.resolve()):
                raise ValueError("unsafe archive path")
        bundle.extractall(checkout)
    spec_path, output = directory / "legacy-spec.json", directory / "legacy.jsonl"
    spec_path.write_text(
        json.dumps(
            [{"case": c, "repeat": repeat} for c in cases for repeat in range(repetitions)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    name = "p5_old_" + uuid4().hex[:12]
    config = dotenv_values(ROOT / ".env")
    url = make_url(config["DATABASE_URL"])
    assert url.host == "127.0.0.1" and url.port == 3307 and url.database == "secondhand_langgraph"
    mysql_admin(
        f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4; "
        f"GRANT ALL ON `{name}`.* TO 'langgraph_app'@'%';"
    )
    # Only explicitly needed app/model fields enter the child; the old .env is never read/copied.
    env = {
        **os.environ,
        **{
            k: v
            for k, v in config.items()
            if v is not None
            and (k.startswith("MODEL_") or k.startswith("AGENT_") or k == "AUTH_SECRET")
        },
        "DATABASE_URL": url.set(database=name).render_as_string(hide_password=False),
        "LANGSMITH_TRACING": "false",
        "OBSERVABILITY_LANGSMITH_ENABLED": "false",
        "AGENT_RUNTIME": "V4",
        "MODEL_MAX_RETRIES": "0",
        "P5_CONCURRENCY": str(concurrency),
        "PYTHONUTF8": "1",
    }
    prices = SemanticModel().prices()
    if prices:
        env.update(
            MODEL_INPUT_PRICE_PER_MILLION=str(prices[0]),
            MODEL_OUTPUT_PRICE_PER_MILLION=str(prices[1]),
            MODEL_CACHED_INPUT_PRICE_PER_MILLION=str(prices[0]),
            MODEL_COST_CURRENCY="CNY",
        )
    try:
        for command in (
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            [
                sys.executable,
                str(Path(__file__).with_name("legacy_worker.py")),
                str(spec_path),
                str(output),
            ],
        ):
            result = subprocess.run(
                command,
                cwd=checkout / "backend",
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3600,
            )
            (directory / f"legacy-{len(list(directory.glob('legacy-*.log')))}.log").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            if result.returncode:
                raise RuntimeError("legacy isolated subprocess failed; inspect local log")
        return [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    finally:
        mysql_admin(f"DROP DATABASE `{name}`; REVOKE ALL ON `{name}`.* FROM 'langgraph_app'@'%';")


def run(factory, postgres_url, options):
    directory = Path(options["output"]).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    all_cases = load_cases()
    cases = [c for c in all_cases if not options.get("select") or c["case_id"] in options["select"]]
    if not options.get("smoke"):
        cases = [c for c in cases if not c.get("smoke")]
    meta = metadata(all_cases)
    report = {
        "schema": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "metadata": meta,
        "options": options,
        "results": [],
        "legacy_results": [],
        "remote": [],
        "release_status": "INSUFFICIENT_EVIDENCE",
    }
    lock = threading.Lock()
    settings = get_settings()
    tracing = (
        trace_client(
            api_url=settings.langsmith_endpoint,
            api_key=settings.langsmith_api_key.get_secret_value(),
        )
        if options.get("remote") and settings.langsmith_api_key
        else None
    )

    def save():
        temp = directory / "report.tmp"
        temp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        temp.replace(directory / "report.json")

    save()

    def execute(case, repeat):
        row = {
            "case_id": case["case_id"],
            "repeat": repeat,
            "target": "graph",
            "status": "OK",
            "auxiliary_calls": [],
            "case_sha256": digest(case),
        }
        target = None
        try:
            target = GraphTarget(factory, postgres_url, case, trace_client=tracing)
            simulation = simulate(target, case, row["auxiliary_calls"])
            row["trajectory"] = simulation["trajectory"]
            if not case.get("smoke"):
                row["judge"] = judge(case, row["trajectory"], row["auxiliary_calls"])
        except Exception as exc:
            row.update(status="ERROR", error_type=type(exc).__name__)
        finally:
            if target:
                try:
                    row.update(target.snapshot())
                    row.update(score_case(case, row))
                except Exception as exc:
                    row.update(
                        status="ERROR", snapshot_error=type(exc).__name__, database_evidence=False
                    )
            if row["status"] != "OK":
                row["business_pass"] = False
            with lock:
                report["results"].append(row)
                with (directory / "results.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                save()
                print(
                    f"P5 {case['case_id']} repeat={repeat} {row['status']} "
                    f"business={row.get('business_pass')}",
                    flush=True,
                )
        return row

    with ThreadPoolExecutor(max_workers=options["concurrency"]) as pool:
        futures = [
            pool.submit(execute, c, repeat)
            for c in cases
            for repeat in range(1 if c.get("smoke") else options["repetitions"])
        ]
        for future in as_completed(futures):
            future.result()
    report["summary"] = summarize(report["results"])
    comparable = [c for c in cases if c["group"] == "business_preserving"]
    if options.get("legacy") and comparable:
        try:
            report["legacy_results"] = run_legacy(
                comparable, options["repetitions"], directory, options["concurrency"]
            )
            by_id = {c["case_id"]: c for c in comparable}
            for old in report["legacy_results"]:
                old["judge"] = judge(by_id[old["case_id"]], old["messages"], old["auxiliary_calls"])
        except Exception as exc:
            report["legacy_error"] = type(exc).__name__
        save()
    if options.get("remote"):
        if tracing:
            try:
                tracing.flush(timeout=10)
                project = tracing.read_project(project_name="secondhand-agent-langgraph-test")
                native = asyncio.run(
                    query_native_traces(
                        tracing,
                        project.id,
                        datetime.fromisoformat(report["started_at"]),
                        {"LangGraph", "parse_intents", "commit_business", "publish_reply"},
                    )
                )
                expected_refs = {
                    digest(r["run_id"]) for row in report["results"] for r in row.get("runs", [])
                }
                refs = {(r.metadata or {}).get("run_ref") for r in native}
                report["native_trace"] = {
                    "status": "PASS" if expected_refs <= refs else "INSUFFICIENT_EVIDENCE",
                    "expected_graph_runs": len(expected_refs),
                    "matched_graph_runs": len(expected_refs & refs),
                    "spans": len(native),
                    "missing_refs": sorted(expected_refs - refs),
                    "example_url": "https://smith.langchain.com" + native[0].app_path
                    if native and native[0].app_path
                    else None,
                }
            except Exception as exc:
                report["native_trace"] = {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "error_type": type(exc).__name__,
                }
        comparable_ids = {c["case_id"] for c in comparable}
        new_comparable = [r for r in report["results"] if r["case_id"] in comparable_ids]
        for label, rows, dataset_label in (
            ("graph", report["results"], "full"),
            ("graph-business", new_comparable, "business"),
            ("legacy-business", report["legacy_results"], "business"),
        ):
            if not rows:
                continue
            try:
                remote = export_experiment(rows, meta, label=label, dataset_label=dataset_label)
            except Exception as exc:
                remote = {"status": "INSUFFICIENT_EVIDENCE", "error_type": type(exc).__name__}
            report["remote"].append(remote)
            remote["target"] = label
            for row in rows:
                row["remote_returned"] = remote["status"] == "PASS"
            save()
    report["risk"] = risk_report(
        all_cases,
        report["results"],
        development_families=[c["family"] for c in all_cases if c["split"] == "development"],
        remote_required=options.get("remote", False),
    )
    rows = report["results"]
    clear = {c["case_id"] for c in cases if c.get("clear_offer")}
    mixed = {c["case_id"] for c in cases if c.get("mixed")}
    report["quality"] = {}
    for key, ids in (("clear_offer_recall", clear), ("mixed_intent_coverage", mixed)):
        subset = [r for r in rows if r["case_id"] in ids]
        report["quality"][key] = (
            sum(r.get("business_pass") is True for r in subset) / len(subset) if subset else None
        )
    report["comparison"] = comparison(comparable, rows, report["legacy_results"])
    report["release_status"] = acceptance(
        report["risk"],
        deterministic="PASS" if rows and all(r.get("business_pass") for r in rows) else "FAIL",
        quality="PASS"
        if all(v is not None and v >= 0.95 for v in report["quality"].values())
        else "INSUFFICIENT_EVIDENCE",
        remote="PASS"
        if report["remote"]
        and all(r["status"] == "PASS" for r in report["remote"])
        and report.get("native_trace", {}).get("status") == "PASS"
        else "INSUFFICIENT_EVIDENCE",
        comparison=report["comparison"]["status"],
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    save()
    return report


def comparison(cases, new, old):
    groups = []
    for case in cases:
        n = [r for r in new if r["case_id"] == case["case_id"]]
        o = [r for r in old if r["case_id"] == case["case_id"]]
        np = sum(r.get("business_pass") is True for r in n)
        op = sum(r.get("business_pass") is True for r in o)
        groups.append(
            {
                "case_id": case["case_id"],
                "new_n": len(n),
                "old_n": len(o),
                "new_pass": np,
                "old_pass": op,
                "status": "INSUFFICIENT_EVIDENCE"
                if not n or len(n) != len(o)
                else "PASS"
                if np >= op
                else "FAIL",
            }
        )
    return {
        "status": "FAIL"
        if any(g["status"] == "FAIL" for g in groups)
        else "PASS"
        if groups and all(g["status"] == "PASS" for g in groups)
        else "INSUFFICIENT_EVIDENCE",
        "cases": groups,
    }
