"""Bounded real-model regressions for buyer intent and the final production reply."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.agent.decision_provider import LangChainDecisionProvider
from app.agent.model_factory import QwenChatModelFactory
from app.agent.runtime_state import freeze_config
from app.core.config import get_settings
from evaluation.dynamic_buyer import BuyerAction
from evaluation.dynamic_runner import DynamicCallTracker
from evaluation.environment import EvaluationEnvironment
from evaluation.production_driver import ProductionDriver
from evaluation.scenarios import load_scenarios

CASES = [
    ("greeting", "你好", {("GENERAL", "GENERAL")}),
    ("product", "我想知道商品信息", {("ASK_FACT", "PRODUCT_DETAILS")}),
    ("negotiability", "是否支持讲价？", {("ASK_FACT", "PRICE_NEGOTIATION")}),
    ("negotiability_paraphrase", "价钱能商量吗？", {("ASK_FACT", "PRICE_NEGOTIATION")}),
    ("listed_price", "页面公开标价是多少？", {("ASK_FACT", "LISTED_PRICE")}),
    ("product_after_price", "先不谈价格，介绍一下成色和配件", {("ASK_FACT", "PRODUCT_DETAILS")}),
    (
        "multiple_questions",
        "电池健康度多少，公开标价多少？",
        {("ASK_FACT", "PRODUCT_DETAILS"), ("ASK_FACT", "LISTED_PRICE")},
    ),
    (
        "multiple_terms",
        "能包邮并保证今天发货吗？",
        {("REQUEST_TERM", "SHIPPING_PAYER"), ("REQUEST_COMMITMENT", "DISPATCH_DEADLINE")},
    ),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--max-cost", type=Decimal, default=Decimal("0.15"))
    args = parser.parse_args()
    if not 1 <= args.trials <= 3 or not Decimal("0") < args.max_cost <= Decimal("0.15"):
        raise ValueError("Probe limits exceed the authorized focused regression scope")
    args.output.mkdir(parents=True, exist_ok=False)
    # This historical probe verifies the original topic/template contract.
    # Open-ended dialogue is verified by scripts.verify_conversation_reply.
    settings = get_settings().model_copy(update={"agent_reply_strategy": "legacy-v1"})
    config = freeze_config(settings)
    if config["prices"]["currency"] != "CNY":
        raise ValueError("Configured CNY model prices are required")
    tracker = DynamicCallTracker(
        LangChainDecisionProvider(QwenChatModelFactory().create(settings)),
        config,
        max_calls=40,
        max_tokens=300000,
        max_cost=args.max_cost,
        timeout=600,
    )
    tracker.journal_path = args.output / "call-journal.jsonl"
    manifest = {
        "model_is_mock": False,
        "started_at": datetime.now(UTC).isoformat(),
        "config": config,
        "max_cost": str(args.max_cost),
        "trials": args.trials,
        "scope": "Real model with production runtime/reply policy, isolated in-memory domain",
    }
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    base = next(s for s in load_scenarios().scenarios if s.scenario_id == "inquiry_01")
    scenario = base.model_copy(
        update={
            "product": base.product.model_copy(
                update={
                    "title": "iPhone 15 Pro 256GB",
                    "description": (
                        "国行原色钛金属，电池健康度 93%，边框轻微使用痕迹，"
                        "配原装包装和充电线。"
                    ),
                    "listed_price": Decimal("5299.00"),
                }
            )
        }
    )
    rows = []
    interrupted = True
    try:
        for trial in range(args.trials):
            driver = ProductionDriver(EvaluationEnvironment(scenario), config, "G4", tracker)
            for case_id, message, expected in CASES:
                tracker.run_id.set(f"dialogue:{trial}:{case_id}")
                result = driver.run_turn(BuyerAction(action="ASK", message=message))
                calls = [
                    c
                    for r in driver.stream
                    if r.task_id == driver.turn and r.kind == "TOOL_CALL"
                    for c in r.payload["message"]["data"].get("tool_calls", [])
                    if c["name"] == "finish_turn"
                ]
                actual = {
                    (act["kind"], act["subject"])
                    for act in calls[-1]["args"]["decision"]["dialogue_acts"]
                } if calls else set()
                passed = (
                    actual == expected
                    and result.outcome.value in {"INFORMATIONAL", "CLARIFICATION"}
                    and not driver.environment.commitments
                    and "价格条件需要通过正式报价提交" not in result.reply
                )
                if case_id in {"product", "product_after_price", "multiple_questions"}:
                    passed = passed and "93" in result.reply
                if case_id in {"listed_price", "multiple_questions"}:
                    passed = passed and "5299" in result.reply
                if case_id in {"negotiability", "negotiability_paraphrase"}:
                    passed = (
                        passed
                        and "可以讨论价格" in result.reply
                        and "评估后确认" in result.reply
                        and result.outcome.value == "INFORMATIONAL"
                    )
                rows.append(
                    {
                        "trial": trial,
                        "case": case_id,
                        "buyer_message": message,
                        "expected_acts": sorted(expected),
                        "actual_acts": sorted(actual),
                        "reply": result.reply,
                        "outcome": result.outcome.value,
                        "passed": passed,
                    }
                )
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
        interrupted = False
    finally:
        calls_text = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in tracker.calls)
        (args.output / "calls.jsonl").write_text(calls_text, encoding="utf-8")
        (args.output / "report.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        passed = not interrupted and len(rows) == len(CASES) * args.trials and all(
            r["passed"] for r in rows
        )
        (args.output / "gate.json").write_text(json.dumps({"passed": passed}), encoding="utf-8")
        manifest.update(
            completed_at=datetime.now(UTC).isoformat(),
            interrupted=interrupted,
            reserved_cost=str(tracker.reserved_cost),
            calls_sha256=hashlib.sha256(calls_text.encode()).hexdigest(),
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    assert passed, "Buyer-intent or final-reply regression failed"


if __name__ == "__main__":
    main()
