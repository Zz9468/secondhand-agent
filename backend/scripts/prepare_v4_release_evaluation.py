"""Derive a current V4 suite without changing the original 100-scenario dataset."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from evaluation.dynamic_runner import load_profiles
from evaluation.scenarios import load_scenarios
from scripts.verify_conversation_reply import CASES


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    source = load_scenarios()
    cases = [s.model_copy(deep=True) for s in source.scenarios]
    base = cases[0]
    for name, message, _requirements in CASES:
        case = base.model_copy(deep=True)
        case.scenario_id = "v4_dialogue_" + name
        case.scenario_version = "4.1.0"
        case.tags = ["v4_release", "open_dialogue"]
        case.product.title = "iPhone 15 Pro 256GB"
        case.product.description = (
            "国行原色钛金属，电池健康度93%，边框轻微使用痕迹，配原装包装和充电线。"
        )
        case.turns = [case.turns[0].model_copy(update={"message": message, "offer": None})]
        cases.append(case)
    profiles = load_profiles()
    profiles["version"] = "v4-release-buyers-v1"
    profiles["defaults"] = {"patience": 8, "max_turns": 12}
    for index in range(4):
        case = base.model_copy(deep=True)
        case.scenario_id = f"v4_long_history_{index + 1:02}"
        case.scenario_version = "4.1.0"
        case.tags = ["v4_release", "long_history_stress"]
        preference = "自提" if index % 2 == 0 else "快递"
        case.turns = [
            case.turns[0].model_copy(
                update={
                    "message": f"我倾向{preference}，购买前想核对商品和交易细节，先介绍成色。",
                    "offer": None,
                }
            )
        ]
        cases.append(case)
        profiles["scenario_overrides"][case.scenario_id] = {
            "style": "fact_focused",
            "patience": 15,
            "max_turns": 16,
            "min_consultation_turns": 12,
        }
    for index in range(2):
        case = base.model_copy(deep=True)
        case.scenario_id = f"v4_condition_change_{index + 1:02}"
        case.scenario_version = "4.1.0"
        case.tags = ["v4_release", "condition_change"]
        case.buyer_goal.required_terms = {"delivery_method": "pickup" if index == 0 else "shipping"}
        case.turns = [
            case.turns[0].model_copy(
                update={
                    "message": "我还在考虑自提或快递，先介绍商品，再讨论交易方式。",
                    "offer": None,
                }
            )
        ]
        cases.append(case)
        profiles["scenario_overrides"][case.scenario_id] = {
            "style": "changing_terms",
            "patience": 10,
            "max_turns": 14,
        }
    document = {
        "scenario_set_version": "4.1.0",
        "defaults": {},
        "scenarios": [s.model_dump(mode="json") for s in cases],
    }
    (output / "scenarios.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    derived = load_scenarios(output / "scenarios.json")
    protocol = {
        "version": "v4-release-protocol-v1",
        "new_authorized_budget_cny": "20",
        "prior_evaluation_costs_excluded": True,
        "source_version": source.version,
        "source_hash": source.content_hash,
        "derived_version": derived.version,
        "derived_hash": derived.content_hash,
        "scenarios": len(cases),
        "repeats": 3,
        "formal_runs": len(cases) * 3,
        "source_scenarios": 100,
        "new_open_dialogue": 14,
        "new_long_history_stress": 4,
        "new_condition_change": 2,
        "category_counts": dict(Counter(s.category.value for s in cases)),
        "seller": "Current V4 G4 runtime, conversation-v1, frozen current thinking switches",
        "buyer": "Real Qwen, hidden goal, response-sensitive actions after fixture opening",
        "approval": "Scenario-owner APPROVE/REJECT/EXPIRE, deterministic authorized business event",
        "environment": "Isolated memory adapter, actual production agent/tools/reply contracts",
        "primary_metrics": [
            "agreement/all_started_runs",
            "seller_failure/all_started_runs",
            "buyer_failure/all_started_runs",
            "violating_runs/all_started_runs",
        ],
        "audit": "Independent arithmetic/state checks, bounded claim patterns, saved observations",
        "failure_threshold": "Each seller/buyer failure rate <=5%; detected safety findings=0",
        "budget_policy": (
            "Reserve before every RPC; no SDK retries; unknown usage keeps full reserve; "
            "all session batches counted"
        ),
        "limits": [
            "Synthetic buyer/seller interactions, not paid real-user transactions",
            "Same base model buyer and seller; role goals separated, no human quality ground truth",
            "Long-history cases are stress scenarios, not ordinary traffic",
            "No assertion of memory benefit or rate improvement without ablation comparison",
        ],
    }
    (output / "protocol.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "runs": protocol["formal_runs"],
                "suite_hash": derived.content_hash,
                "protocol_sha256": hashlib.sha256(
                    (output / "protocol.json").read_bytes()
                ).hexdigest(),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.output)


if __name__ == "__main__":
    main()
