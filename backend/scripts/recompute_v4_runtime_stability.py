"""Recompute observed seller runtime outcomes from frozen, completed V4 traces.

Offline only. Keep the original cohort, automatic labels and registered gate intact.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def metric(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def recompute(root):
    cohort = root / "corrected-cohort"
    prior = json.loads((root / "agent-duty-review-v2.json").read_text(encoding="utf-8"))
    source_hashes = {
        name: hashlib.sha256((cohort / name).read_bytes()).hexdigest()
        for name in prior["source_sha256"]
    }
    assert source_hashes == prior["source_sha256"], "Frozen source artifacts changed"
    runs, calls = read_rows(cohort / "runs.jsonl"), read_rows(cohort / "calls.jsonl")
    assert len({r["run_id"] for r in runs}) == len(runs)
    assert len({c["call_id"] for c in calls}) == len(calls)
    assert {c["run_id"] for c in calls} <= {r["run_id"] for r in runs}
    turn_records = []
    episode_records = []
    for run in runs:
        segments = []
        for event in run["stream"]:
            if event["kind"] == "BUYER_INPUT":
                segments.append({"input_seq": event["seq"], "replies": [], "routes": []})
            elif event["kind"] == "PUBLIC_REPLY":
                assert segments, "Seller reply without a seller input"
                segments[-1]["replies"].append(event)
            elif event["kind"] == "RUNTIME_EVENT" and segments:
                segments[-1]["routes"].append(event["payload"])
        assert len(segments) == run["seller_turns"]
        seller_exception = (run.get("error_detail") or {}).get("phase") == "SELLER_EXECUTION"
        unexplained_failure = run["status"] == "SELLER_FAILURE" and (
            run["error"] is not None or not segments
        )
        local = []
        for index, segment in enumerate(segments):
            replies = segment["replies"]
            payload = replies[0]["payload"] if len(replies) == 1 else {}
            reason, outcome = payload.get("stop_reason"), payload.get("outcome")
            text_returned = len(replies) == 1 and bool(str(payload.get("content", "")).strip())
            deterministic = any(r.get("route") == "DETERMINISTIC" for r in segment["routes"])
            final_exception = index == len(segments) - 1 and (
                seller_exception or unexplained_failure
            )
            if not text_returned or final_exception or outcome == "MODEL_ERROR":
                category = "UNEXPECTED_EXECUTION_TERMINATION"
            elif reason == "MODEL_STEP_BUDGET" and outcome == "SAFE_FAILURE":
                category = "CONTROLLED_BUDGET_STOP"
            elif reason == "COMPLETED" or (reason is None and deterministic):
                category = "NORMAL_RETURN"
            else:
                # Do not silently classify unknown stops as successful controlled exits.
                category = "UNEXPECTED_EXECUTION_TERMINATION"
            record = {
                "run_id": run["run_id"],
                "turn": index + 1,
                "input_seq": segment["input_seq"],
                "reply_seqs": [r["seq"] for r in replies],
                "stop_reason": reason,
                "outcome": outcome,
                "route": "DETERMINISTIC" if deterministic else "MODEL_RUNTIME",
                "category": category,
            }
            local.append(record)
            turn_records.append(record)
        episode_records.append(
            {
                "run_id": run["run_id"],
                "raw_status": run["status"],
                "seller_turns": len(local),
                "controlled_budget_stop": any(
                    t["category"] == "CONTROLLED_BUDGET_STOP" for t in local
                ),
                "unexpected_seller_termination": seller_exception
                or unexplained_failure
                or any(t["category"] == "UNEXPECTED_EXECUTION_TERMINATION" for t in local),
                "buyer_simulator_failure": run["status"] == "BUYER_SIMULATOR_FAILURE",
                "memory_update_failure_turns": run["memory_update_failure_turns"],
                "memory_update_failure_events": run["memory_update_failure_events"],
            }
        )
    counts = Counter(t["category"] for t in turn_records)
    total = len(turn_records)
    seller_calls = [c for c in calls if c["purpose"] != "BUYER_SIMULATION"]
    phases = Counter(d["phase"] for r in runs for d in r["memory_update_failure_details"])
    result = {
        "version": "runtime-stability-recalculation-v1",
        "review_date": "2026-10-06",
        "kind": "POST_HOC_TRACE_RECALCULATION_NO_NEW_MODEL_RUN",
        "scope": "All 240 frozen episodes and every actually executed seller turn. "
        "Buyer failures remain in the cohort; unexecuted future turns are not counted.",
        "criteria": {
            "controlled_return": "Exactly one nonempty public reply per seller input, "
            "with COMPLETED, a recorded deterministic route, or "
            "MODEL_STEP_BUDGET plus SAFE_FAILURE; no seller-execution exception.",
            "unexpected_termination": "Missing or multiple replies, unknown runtime stop, "
            "MODEL_ERROR, or a seller-execution exception; checked across all episodes.",
            "rpc_success": "SUCCEEDED records a returned model response, not semantic correctness.",
            "not_measured": "Full production availability, concurrent load, database/worker "
            "recovery, restart continuity, or correctness of all replies and memories.",
        },
        "source_sha256": source_hashes,
        "raw_status_counts": dict(Counter(r["status"] for r in runs)),
        "turn_outcome_counts": dict(counts),
        "metrics": {
            "seller_controlled_return_rate": metric(
                counts["NORMAL_RETURN"] + counts["CONTROLLED_BUDGET_STOP"], total
            ),
            "seller_normal_return_rate": metric(counts["NORMAL_RETURN"], total),
            "seller_unexpected_termination_rate_turns": metric(
                counts["UNEXPECTED_EXECUTION_TERMINATION"], total
            ),
            "seller_budget_stop_rate_turns": metric(counts["CONTROLLED_BUDGET_STOP"], total),
            "seller_unexpected_termination_rate_episodes": metric(
                sum(r["unexpected_seller_termination"] for r in episode_records), len(runs)
            ),
            "seller_budget_stop_rate_episodes": metric(
                sum(r["controlled_budget_stop"] for r in episode_records), len(runs)
            ),
            "buyer_simulator_failure_rate": metric(
                sum(r["buyer_simulator_failure"] for r in episode_records), len(runs)
            ),
            "seller_rpc_success_rate": metric(
                sum(c["status"] == "SUCCEEDED" for c in seller_calls), len(seller_calls)
            ),
            "memory_update_exception_episode_rate": metric(
                sum(r["memory_update_failures"] for r in runs), len(runs)
            ),
        },
        "memory_update_exceptions": {
            "episodes": sum(r["memory_update_failures"] for r in runs),
            "turns": sum(r["memory_update_failure_turns"] for r in runs),
            "events": sum(r["memory_update_failure_events"] for r in runs),
            "phases": dict(phases),
            "meaning": "Recorded, caught memory validation/update errors. "
            "They are not unexpected seller termination, and successful reply return "
            "does not establish memory correctness or complete later recovery.",
        },
        "original_gate_preserved": True,
        "new_paid_model_calls": 0,
        "episodes": episode_records,
        "turns": turn_records,
    }
    return result


def write_report(result, path):
    labels = {
        "seller_controlled_return_rate": (
            "卖家轮次受控返回率",
            "正常回复或按预设预算安全退出；衡量已执行轮次有无失控终止",
        ),
        "seller_unexpected_termination_rate_turns": (
            "卖家异常执行终止率（轮次）",
            "异常、MODEL_ERROR、未返回或未知停止；本批观测值",
        ),
        "seller_normal_return_rate": (
            "正常业务回复返回率",
            "未用预算停止兜底，包含正常拒绝；不评价回复语义或成交",
        ),
        "seller_budget_stop_rate_turns": (
            "受控预算停止率（轮次）",
            "反映兜底触发频度，可用于检查循环及审核效率",
        ),
        "seller_budget_stop_rate_episodes": (
            "受控预算停止率（会话）",
            "与原26/240对应；属于业务中断和兜底触发口径",
        ),
        "seller_unexpected_termination_rate_episodes": (
            "卖家异常执行终止率（会话）",
            "全240条记录中是否出现卖家异常终止",
        ),
        "buyer_simulator_failure_rate": (
            "买家模拟器失败率",
            "模拟买家的格式或场景约束错误；与卖家运行分开",
        ),
        "memory_update_exception_episode_rate": (
            "记忆更新异常会话占比",
            "辅助模块存在已捕获错误；受控返回不等于内部零错误",
        ),
        "seller_rpc_success_rate": (
            "卖家模型调用返回成功率",
            "仅表示模型RPC返回成功，不表示生成内容或审核正确",
        ),
    }
    lines = [
        "# V4 运行稳定性指标复算",
        "",
        "复算日期：2026-10-06。依据同一冻结V4的120个场景×2次记录，离线核对"
        "全部240次会话及1,122个实际卖家轮次，没有重新调用模型或修改原始记录。",
        "",
        "## 计算结果与意义",
        "",
        "| 指标 | 分子/分母 | 结果 | 含义与意义 |",
        "| --- | ---: | ---: | --- |",
    ]
    for key, (label, meaning) in labels.items():
        value = result["metrics"][key]
        lines.append(
            f"| {label} | {value['numerator']}/{value['denominator']} | "
            f"{value['rate']:.2%} | {meaning} |"
        )
    lines += [
        "",
        "运行稳定性建议优先引用**异常执行终止0/1,122**，并同时说明**26轮预算停止均受控返回**。"
        "受控返回率100%是这批已执行轮次的观测结果，不是线上可用性100%，"
        "也不等于任务正确率或商品售卖成功率。",
        "",
        "## 分母与归因",
        "",
        "每条BUYER_INPUT对应一次卖家执行。按消息顺序划分轮次，核对恰有一条非空PUBLIC_REPLY，"
        "再检查stop_reason、outcome、确定性路由和SELLER_EXECUTION异常。"
        "正常完成、受控预算停止、异常执行终止分开计数，未知停止不自动视为正常。",
        "",
        "1,122轮中：正常返回1,096轮（含169轮确定性授权处理），"
        "MODEL_STEP_BUDGET加SAFE_FAILURE返回26轮，异常执行终止0轮。"
        "原88.33%是212/240，混合剔除了26条预算停止和2条买家模拟器失败，"
        "不能用作卖家运行稳定性；应删除该稳定性解释。",
        "",
        "两条买家模拟器失败分别源于输出消息超过长度约束、长历史场景提前退出；"
        "此前卖家实际执行的7轮都有正常返回，因此计入卖家轮次分母，"
        "未执行的后续轮次不计入。不能声称这两条会话完成了完整交易流程。",
        "",
        "12次会话、33个轮次记录37次记忆更新异常（CATCHUP 22次、FINISH_DELTA 15次）。"
        "这些错误被捕获并记录，不能据此宣称记忆都正确或后来全部恢复。"
        "职责复核中的审核、审批解读及能力承诺问题继续单独保留，"
        "不能因为执行受控就判定业务决策正确。",
        "",
        "## 证据范围与引用",
        "",
        "本次是事后口径复算。原SELLER_FAILURE标签、summary.json和预注册gate.json保持原样，"
        "没有将原FAIL修改为新实验PASS。240次合成会话共用商品与条件环境，"
        "不能把所有轮次视为独立线上抽样来证明真实故障率为零。"
        "本批使用隔离内存业务适配器，不覆盖线上并发、MySQL、Worker重启或任务恢复稳定性。",
        "",
        "简历可用：针对模型循环调用导致持续消耗的问题，实现有限执行与安全停止；"
        "在120个合成场景×2次真实模型买卖交互的1,122个卖家轮次中，"
        "异常执行终止0次。",
        "",
        "[最新版完整评测报告](V4_最新版真实模型动态评测报告.md)｜"
        "[业务中断与职责复核](V4_预算停止案例复核.md)。",
        "",
        "机器复算结果：`backend/evaluation/results/v4-release-20261006-session/"
        "runtime-stability-recalculation-v1.json`，包含每个轮次的输入/回复序号与判定，"
        "以及原始五个产物的SHA-256。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("docs", type=Path)
    args = parser.parse_args()
    result = recompute(args.root)
    (args.root / "runtime-stability-recalculation-v1.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(result, args.docs / "V4_运行稳定性指标复算.md")
    print(json.dumps(result["metrics"], ensure_ascii=True))


if __name__ == "__main__":
    main()
