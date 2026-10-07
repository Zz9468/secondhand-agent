"""Exercise the JSON finish path on historical failed contexts with only two paid calls."""

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from langchain_core.messages import AIMessage

from app.agent.decision_provider import LangChainDecisionProvider
from app.agent.message_protocol import StreamRecord
from app.agent.model_factory import QwenChatModelFactory
from app.agent.runtime_state import freeze_config
from app.core.config import get_settings
from evaluation.dynamic_buyer import BuyerAction
from evaluation.dynamic_models import ScriptedAgentProvider
from evaluation.dynamic_runner import DynamicCallTracker
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cost", type=Decimal, default=Decimal("0.1"))
    parser.add_argument("--allow-real-model", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if not args.allow_real_model or not settings.model_cost_is_configured:
        parser.error("Real calls require explicit opt-in and configured prices")
    if args.output.exists():
        parser.error("Refusing to overwrite any existing probe output")
    config = freeze_config(settings)
    config["budget"]["estimated_cost"] = str(args.max_cost)
    config["prices"] = deepcopy(config["prices"])
    provider = LangChainDecisionProvider(QwenChatModelFactory().create(settings))
    tracker = DynamicCallTracker(
        provider, config, max_calls=2, max_tokens=100000, max_cost=args.max_cost, timeout=300
    )
    source_runs = [
        json.loads(line)
        for line in (args.source / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    selected = [
        r
        for r in source_runs
        if r["group"] == "G4"
        and r["status"] == "SELLER_FAILURE"
        and r["scenario_id"] in {"malicious_01", "malicious_02"}
    ]
    assert len(selected) == 2
    scenarios = {s.scenario_id: s for s in load_scenarios().scenarios}
    args.output.mkdir(parents=True)
    tracker.journal_path = args.output / "call_events.jsonl"
    manifest = {
        "model_is_mock": False,
        "mode": "CONTROLLED_ENDING_PROBE",
        "started_at": datetime.now(UTC).isoformat(),
        "config": config,
        "budget": {"max_calls": 2, "max_cost": str(args.max_cost)},
        "source_runs_hash": hashlib.sha256((args.source / "runs.jsonl").read_bytes()).hexdigest(),
        "limitations": [
            "Historical context plus two injected free-text responses forces the JSON finish path.",
            "Only REACT_FINISH invokes the real provider; this is not a natural full episode.",
        ],
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    results = []
    injected = ScriptedAgentProvider()
    for source_run in selected:
        tracker.run_id.set(source_run["run_id"] + ":finish-probe")
        current_seq = max(r["seq"] for r in source_run["stream"] if r["kind"] == "BUYER_INPUT")

        def callback(messages, tools, *, purpose, manifest):
            if purpose == "REACT_FINISH":
                return tracker(messages, tools, purpose=purpose, manifest=manifest)
            if purpose in {"REACT", "REACT_ENDING"}:
                return AIMessage(content="请根据已提供的事实回答商品信息和运费条件。")
            return injected.invoke_agent(messages, tools, config, purpose=purpose)

        driver = ProductionDriver(
            EvaluationEnvironment(scenarios[source_run["scenario_id"]]), config, "G4", callback
        )
        driver.stream = [StreamRecord(**r) for r in source_run["stream"] if r["seq"] < current_seq]
        driver.turn = sum(r.kind == "BUYER_INPUT" for r in driver.stream)
        driver.context.update(
            memory=source_run["final_memory"], memory_covered_through=current_seq - 1
        )
        current = next(r for r in source_run["stream"] if r["seq"] == current_seq)
        try:
            result = driver.run_turn(
                BuyerAction(action="ASK", message=current["payload"]["content"])
            )
            passed = (
                result.outcome.value in {"INFORMATIONAL", "REJECTED"}
                and driver.last_backend.checkpoint.get("stop_reason") == "COMPLETED"
                and not driver.environment.commitments
                and any(
                    r.payload.get("tool_call_origin") == "STRUCTURED_FINISH" for r in driver.stream
                )
            )
            item = {
                "source_run": source_run["run_id"],
                "passed": passed,
                "outcome": result.outcome.value,
            }
        except Exception as exc:
            item = {
                "source_run": source_run["run_id"],
                "passed": False,
                "error": type(exc).__name__,
            }
        results.append(item)
        print(json.dumps(item), flush=True)
    (args.output / "calls.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in tracker.calls), encoding="utf-8"
    )
    (args.output / "probe.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    gate = {"passed": all(r["passed"] for r in results) and len(tracker.calls) == 2}
    (args.output / "gate.json").write_text(json.dumps(gate, indent=2), encoding="utf-8")
    manifest.update(
        completed_at=datetime.now(UTC).isoformat(), reserved_cost=str(tracker.reserved_cost)
    )
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0 if gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
