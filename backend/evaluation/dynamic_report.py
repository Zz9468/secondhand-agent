"""Reproducible paired summaries and a blinded review packet from saved dynamic runs."""

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from evaluation.dynamic_metrics import summarize_dynamic


def paired_intervals(runs, repeats=2000):
    by_case = defaultdict(dict)
    for run in runs:
        by_case[(run["scenario_id"], run["trial"])][run["group"]] = run
    result = {}
    for a, b in (("G1", "G0"), ("G2", "G1"), ("G3", "G1"), ("G4", "G2"), ("G4", "G3")):
        differences = defaultdict(list)
        for (case, _trial), groups in by_case.items():
            if a in groups and b in groups and groups[a]["feasible"]:
                differences[case].append(
                    int(groups[a]["status"] == "AGREED") - int(groups[b]["status"] == "AGREED")
                )
        means = [sum(values) / len(values) for values in differences.values()]
        if not means:
            continue
        rng = random.Random(20261006)
        bootstrap = sorted(
            sum(rng.choice(means) for _ in means) / len(means) for _ in range(repeats)
        )
        result[f"{a}-{b}"] = {
            "metric": "feasible_agreement_rate_difference",
            "difference": sum(means) / len(means),
            "scenario_cluster_bootstrap_95_interval": [
                bootstrap[int(repeats * 0.025)],
                bootstrap[int(repeats * 0.975)],
            ],
            "scenarios": len(means),
        }
    return result


def call_calibration(calls, output_reserve=1024):
    ratios = []
    for call in calls:
        usage = call.get("usage")
        if usage and usage.get("input_tokens"):
            reserved_input = call.get(
                "estimated_input_tokens",
                call.get("initial_reserved_tokens", call["reserved_tokens"])
                - call["request"].get("max_output_tokens", output_reserve),
            )
            ratios.append(reserved_input / usage["input_tokens"])
    return {
        "calls_with_known_input": len(ratios),
        "minimum_estimate_to_actual_input_ratio": min(ratios) if ratios else None,
        "underestimated_calls": sum(ratio < 1 for ratio in ratios),
        "estimator": "UTF-8 bytes/2 plus message overhead; recorded output reserve",
    }


def trace_metrics(runs):
    result = {}
    for group in sorted({r["group"] for r in runs}):
        seen, repeat, calls, internal, memory, valid_sources = set(), 0, 0, 0, 0, 0
        for run in (r for r in runs if r["group"] == group):
            sources = {r["seq"] for r in run["stream"]}
            for row in run["stream"]:
                if row["kind"] == "TOOL_CALL":
                    for call in row["payload"]["message"]["data"]["tool_calls"]:
                        if row["payload"].get("tool_call_origin") == "STRUCTURED_FINISH":
                            internal += 1
                            continue
                        calls += 1
                        key = (
                            run["run_id"],
                            row["task_id"],
                            call["name"],
                            json.dumps(call["args"], sort_keys=True),
                        )
                        if key in seen and call["name"] in {
                            "evaluate_offer",
                            "get_product_info",
                            "get_negotiation_state",
                        }:
                            repeat += 1
                        seen.add(key)
            for entry in run["final_memory"].get("entries", []):
                memory += 1
                valid_sources += bool(entry["source_seqs"]) and set(entry["source_seqs"]).issubset(
                    sources
                )
        result[group] = {
            "native_tool_calls": calls,
            "internal_finish_calls": internal,
            "identical_read_calls_in_same_turn": repeat,
            "memory_entries": memory,
            "memory_source_exists_rate": valid_sources / memory if memory else None,
        }
    return result


def purpose_usage(calls):
    result = {}
    for purpose in sorted({call["purpose"] for call in calls}):
        selected = [call for call in calls if call["purpose"] == purpose]
        result[purpose] = {
            "calls": len(selected),
            "known_input_tokens": sum((c["usage"] or {}).get("input_tokens", 0) for c in selected),
            "known_output_tokens": sum(
                (c["usage"] or {}).get("output_tokens", 0) for c in selected
            ),
            "unknown_usage_calls": sum(c["usage"] is None for c in selected),
            "known_estimated_cost": str(
                sum(
                    (
                        Decimal(c["estimated_cost"])
                        for c in selected
                        if c["estimated_cost"] is not None
                    ),
                    Decimal("0"),
                )
            ),
        }
    return result


def write_report(output):
    runs = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    calls = [
        json.loads(line)
        for line in (output / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    summary = summarize_dynamic(runs, calls)
    analysis = {
        "groups": summary,
        "paired_intervals": paired_intervals(runs),
        "token_calibration": call_calibration(calls, manifest["config"]["budget"]["output_tokens"]),
        "trace_metrics": trace_metrics(runs),
        "usage_by_purpose": purpose_usage(calls),
        "runtime_stop_reasons": dict(
            Counter(
                row["payload"]["stop_reason"]
                for run in runs
                for row in run["stream"]
                if row["kind"] == "PUBLIC_REPLY"
                and row["payload"].get("outcome") == "SAFE_FAILURE"
                and row["payload"].get("stop_reason")
            )
        ),
        "not_automatically_scored": [
            "fact correctness beyond exact structured facts",
            "question answered quality",
            "condition change semantic correctness",
            "naturalness",
            "persuasion",
            "irrelevant tool use",
        ],
    }
    (output / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = [
        "# V4 动态交互对照结果",
        "",
        f"原数据集：{manifest['scenario_set_version']}；运行 {len(runs)} 次。"
        f"模型：{manifest['config']['model']['name']}。",
        "",
        "| 组别 | 意向达成 | 有效结束 | 卖家失败 | 买家失败 | 已知费用/元 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group, metrics in summary.items():
        agreement = metrics["agreement_rate_on_feasible"]
        agreement_text = f"{agreement:.1%}" if agreement is not None else "—"
        text.append(
            f"| {group} | {agreement_text} | "
            f"{metrics['effective_end_rate']:.1%} | {metrics['seller_failure_rate']:.1%} | "
            f"{metrics['buyer_failure_rate']:.1%} | {metrics['known_estimated_cost']} |"
        )
    text.extend(
        [
            "",
            "全部失败调用与模拟买家、摘要、记忆调用均保留；未知用量不算作零费用。费用为冻结单价估算，最终账单以提供商为准。",
            "",
            "配对差值及按场景聚类的 bootstrap 区间见 analysis.json。"
            "小样本和模型采样波动不能证明真实成交收益。"
            "自然度、问题回答质量和复杂事实正确性尚未获得人工标注，"
            "留有匿名复核材料，不给出未经校准的自动分数。",
            "",
            "## 失败分布",
            "",
        ]
    )
    failures = Counter(
        (r["group"], r["status"], r["error"])
        for r in runs
        if r["status"] in {"SELLER_FAILURE", "BUYER_SIMULATOR_FAILURE"}
    )
    text.extend(
        f"- {group} / {status} / {error or 'bounded runtime stop'}：{count}"
        for (group, status, error), count in sorted(failures.items(), key=str)
    )
    text.extend(
        [
            "",
            "## 用量按用途拆分",
            "",
            "| 用途 | 调用数 | 已知输入 Token | 已知输出 Token | 未知用量 | 已知费用/元 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for purpose, values in analysis["usage_by_purpose"].items():
        text.append(
            f"| {purpose} | {values['calls']} | {values['known_input_tokens']} | "
            f"{values['known_output_tokens']} | {values['unknown_usage_calls']} | "
            f"{values['known_estimated_cost']} |"
        )
    text.extend(["", f"有记录的有界停止原因：{analysis['runtime_stop_reasons']}。"])
    (output / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    products = {}
    for call in calls:
        for message in call["request"]["messages"]:
            content = message["data"]["content"]
            if isinstance(content, str) and content.startswith("后端业务事实和来源清单"):
                data = json.loads(content.split("\n", 1)[1])
                products[call["run_id"]] = data["facts"]["product"]
    candidates = []
    for run in runs:
        for index, row in enumerate(run["stream"]):
            if row["kind"] == "PUBLIC_REPLY":
                buyer = next(
                    (
                        r["payload"]["content"]
                        for r in reversed(run["stream"][:index])
                        if r["kind"] == "BUYER_INPUT"
                    ),
                    "",
                )
                review_id = hashlib.sha256(f"{run['run_id']}:{row['seq']}".encode()).hexdigest()[
                    :16
                ]
                candidates.append(
                    {
                        "review_id": review_id,
                        "buyer_message": buyer,
                        "seller_reply": row["payload"]["content"],
                        "public_product_facts": products.get(run["run_id"]),
                        "recent_public_history": [
                            {"kind": r["kind"], "content": r["payload"]["content"]}
                            for r in run["stream"][:index]
                            if r["visibility"] == "PUBLIC" and "content" in r["payload"]
                        ][-6:],
                        "scores": {
                            "fact_support": None,
                            "answers_question": None,
                            "naturalness": None,
                            "persuasion": None,
                        },
                        "_group": run["group"],
                        "_run_id": run["run_id"],
                    }
                )
    random.Random(20261006).shuffle(candidates)
    packet = candidates[:100]
    rubric = {
        "version": "v4-blind-review-v1",
        "status": "AWAITING_INDEPENDENT_HUMAN_ANNOTATION",
        "sampling": "100 public replies, fixed shuffle seed 20261006; group key stored separately",
        "scale": {"1": "严重问题", "3": "基本合格但有明显不足", "5": "准确且充分"},
        "criteria": {
            "fact_support": "是否仅表达商品记录或公开业务结果支持的事实；证据不足留空并备注",
            "answers_question": "是否回答本轮明确问题，或准确指出缺失信息并提出相关澄清",
            "naturalness": "是否清楚、简洁、连贯，避免重复询问、机械套话或内部术语",
            "persuasion": "是否在授权范围内解释价值、提出相关可行方案并推进下一步",
        },
        "procedure": "标注时不查看 group key；建议两人独立评分并复核差异。"
        "事实项须对照原场景商品记录和原始流；未标注项不计零分。",
    }
    (output / "blind_review_rubric.json").write_text(
        json.dumps(rubric, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "blind_review_key.json").write_text(
        json.dumps(
            [{k: r[k] for k in ("review_id", "_group", "_run_id")} for r in packet], indent=2
        ),
        encoding="utf-8",
    )
    with (output / "blind_review.jsonl").open("w", encoding="utf-8") as file:
        for row in packet:
            file.write(
                json.dumps(
                    {k: v for k, v in row.items() if not k.startswith("_")}, ensure_ascii=False
                )
                + "\n"
            )
    return {
        "runs": len(runs),
        "known_cost": str(
            sum(Decimal(c["estimated_cost"]) for c in calls if c["estimated_cost"] is not None)
        ),
        "calibration": analysis["token_calibration"],
        "report": str(output / "report.md"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(write_report(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
