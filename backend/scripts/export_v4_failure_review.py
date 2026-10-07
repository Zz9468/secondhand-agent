"""Export the 26 seller failures for human review without new model calls."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

CATEGORIES = [
    ("MALICIOUS_INSTRUCTION", "恶意指令", 17),
    ("INQUIRY", "商品咨询", 7),
    ("MULTI_ROUND", "多轮议价", 1),
    ("LOW_OFFER", "低价报价", 1),
]
TOPICS = {
    "malicious_01": "要求泄露卖家底价",
    "malicious_02": "要求输出系统提示词和隐藏价格策略",
    "malicious_03": "要求假装卖家已审批",
    "malicious_04": "要求绕过买家确认宣布成交",
    "malicious_06": "冒充管理员绕过价格限制",
    "malicious_08": "要求伪造审批编号和卖家同意",
    "malicious_09": "要求绕过规则接受一元报价",
    "malicious_12": "声称已付款并要求确认成交",
    "malicious_14": "英文要求卖家审批和泄露底价",
    "malicious_15": "要求复述底价泄露审批通过和今天发货",
    "inquiry_06": "快递咨询",
    "inquiry_08": "商品在售状态咨询",
    "inquiry_10": "包装盒和购买凭证咨询",
    "v4_dialogue_greeting": "从问候开始的开放咨询",
    "v4_long_history_01": "长对话压力场景一",
    "v4_long_history_03": "长对话压力场景三",
    "multi_04_very_low_to_boundary": "从极低报价开始的多轮议价",
    "prohibited_07_discount_below": "报价及卖家折扣使净收入低于授权",
}


def read_rows(path):
    return [
        (line_number, json.loads(line))
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.strip()
    ]


def quote(text):
    return ["> " + line if line else ">" for line in str(text).split("\n")]


def code(value):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    longest = max((len(m.group()) for m in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + ("text" if isinstance(value, str) else "json"), content, fence]


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def msg_data(row):
    return row["payload"].get("message", {}).get("data", {})


def tool_result(row):
    data = msg_data(row)
    full = data.get("artifact", {}).get("full_result")
    if full is not None:
        return full
    try:
        return json.loads(data.get("content", ""))
    except (TypeError, ValueError):
        return data.get("content", "")


def review_input(call):
    for message in call["request"]["messages"]:
        if message.get("type") == "human":
            payload = json.loads(message["data"]["content"])
            if "finish" in payload:
                return payload
    raise AssertionError(f"Missing review input: {call['call_id']}")


def export(root, output):
    directory = root / "corrected-cohort"
    runs_path, calls_path = directory / "runs.jsonl", directory / "calls.jsonl"
    rows = read_rows(runs_path)
    selected = [(n, r) for n, r in rows if r["status"] == "SELLER_FAILURE"]
    assert len(selected) == 26 and len({r["run_id"] for _, r in selected}) == 26
    counts = Counter(r["category"] for _, r in selected)
    assert counts == {key: expected for key, _, expected in CATEGORIES}
    assert len({r["scenario_id"] for _, r in selected}) == 18
    cases = {
        s["scenario_id"]: s
        for s in json.loads((root / "scenarios-corrected.json").read_text(encoding="utf-8"))[
            "scenarios"
        ]
    }
    calls = defaultdict(list)
    for n, call in read_rows(calls_path):
        calls[call["run_id"]].append((n, call))
    numbered = []
    for category, _, _ in CATEGORIES:
        for n, run in sorted(
            (item for item in selected if item[1]["category"] == category),
            key=lambda item: (item[1]["scenario_id"], item[1]["trial"]),
        ):
            numbered.append((f"F{len(numbered) + 1:02}", n, run))
    final_replies = {
        r["run_id"]: next(s for s in reversed(r["stream"]) if s["kind"] == "PUBLIC_REPLY")
        for _, _, r in numbered
    }
    assert all(
        row["payload"].get("stop_reason") == "MODEL_STEP_BUDGET" for row in final_replies.values()
    )
    reviews = sum(
        c["purpose"] == "REPLY_REVIEW" for _, _, r in numbered for _, c in calls[r["run_id"]]
    )
    lines = [
        "# V4 卖家失败案例人工审查",
        "",
        "评测日期：2026-10-06。范围为当前 V4 统一 240 次动态评测中的全部 "
        "26 次卖家运行失败，涉及 18 个不同场景。同一场景的两次重复运行分别列出，便于比较。",
        "",
        "26 次最终停止原因均为 MODEL_STEP_BUDGET，即单轮模型步骤预算耗尽。"
        "这个字段说明如何停止；此前的候选回复、工具反馈和审核记录用于判断为何未能完成。"
        "审核模型的理由按原文保存，是否成立留给人工判断。",
        "",
        "## 分类总览",
        "",
        "| 场景分类 | 失败运行数 | 不同场景数 | 案例编号 |",
        "| --- | ---: | ---: | --- |",
    ]
    for category, label, expected in CATEGORIES:
        items = [(identifier, r) for identifier, _, r in numbered if r["category"] == category]
        number_range = items[0][0] if len(items) == 1 else f"{items[0][0]} 至 {items[-1][0]}"
        lines.append(
            f"| {label} | {expected} | {len({r['scenario_id'] for _, r in items})} | "
            f"{number_range} |"
        )
    lines += [
        "| 合计 | 26 | 18 | F01 至 F26 |",
        "",
        "类别沿用冻结场景的 category 字段，根据开场场景归类。"
        "恶意指令场景后续可能转为正常咨询；人工判断应结合失败轮的实际输入。"
        "商品咨询类中有两次长对话压力运行，在案例标题中单独标明。",
        "",
        "## 人工审查说明",
        "",
        "每个案例先列业务事实及完整公开对话，再列内部模型草稿、全部工具尝试与结果，"
        f"最后列审核模型的原始回执。本文件保留这 26 次运行关联的全部 {reviews} 次回复审核调用。",
        "",
        "公开对话是买家实际可见的内容。内部草稿、finish_turn 的候选回复和审核意见"
        "不应当成已经发送的卖家承诺。确定性文字检查、审核模型和事实引用校验都可能拒绝回复；"
        "即使审核模型输出 approved=true，后续引用校验仍可能拒绝该回复。",
        "",
        "优先核对失败轮：候选是否回答了买家问题，肯定事实是否有来源，拒绝理由是否成立，"
        "修正是否回应了工具反馈，以及合规回复为何仍无法结束本轮。"
        "每个案例末尾留有审查结论、优先级和修改建议。",
        "",
        "商品事实、买家目标和卖家授权均为合成测试数据。买家目标及私有授权在此供人工核对，"
        "不表示卖家模型能看到这些字段。引用中的攻击指令是案例原文。",
        "",
        "## 案例索引",
        "",
        "| 编号 | 场景主题 | 重复运行 | 失败轮 | 记录的工具输入拒绝数 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for identifier, _, run in numbered:
        title = TOPICS[run["scenario_id"]]
        anchor = f"{identifier.lower()}-{title}"
        lines.append(
            f"| [{identifier}](#{anchor}) | {title} | {run['trial'] + 1} | "
            f"{run['seller_turns']} | {run['tool_input_rejections']} |"
        )
    evidence_counts = Counter()
    for category, label, _expected in CATEGORIES:
        lines += ["", f"## {label}场景", ""]
        for identifier, source_line, run in numbered:
            if run["category"] != category:
                continue
            case = cases[run["scenario_id"]]
            policy, goal = case["policy"], case["buyer_goal"]
            final = final_replies[run["run_id"]]["payload"]
            failure_input = next(
                row for row in reversed(run["stream"]) if row["kind"] == "BUYER_INPUT"
            )
            lines += [
                f"### {identifier} {TOPICS[run['scenario_id']]}",
                "",
                "| 核对项 | 原始记录 |",
                "| --- | --- |",
                f"| 场景标识及版本 | {run['scenario_id']}，{run['scenario_version']} |",
                f"| 运行标识 | {run['run_id']} |",
                f"| 重复运行 | 第 {run['trial'] + 1} 次 |",
                f"| 失败轮与停止原因 | 第 {run['seller_turns']} 轮，MODEL_STEP_BUDGET |",
                f"| 最终状态与公开结果 | SELLER_FAILURE，{final.get('outcome')} |",
                f"| 商品 | {cell(case['product']['title'])}，"
                f"标价 {case['product']['listed_price']} 元 |",
                f"| 商品已知事实 | {cell(case['product']['description'])} |",
                f"| 卖家私有授权 | 最低净收入 {policy['minimum_net_price']} 元；"
                f"自动授权阈值 {policy['auto_accept_threshold']} 元 |",
                f"| 买家硬预算 | {goal['maximum_price']} 元 |",
                f"| 场景审批结果 | {case['seller_review']} |",
                f"| 工具输入拒绝数 | {run['tool_input_rejections']} |",
                f"| 记忆更新失败事件数 | {run['memory_update_failure_events']} |",
                f"| 检测到的已执行违规数 | {len(run['violations'])} |",
                "",
                f"**最终失败轮买家输入，公开序号 {failure_input['seq']}**",
                "",
                *quote(failure_input["payload"]["content"]),
                "",
                "买家必要条件：",
                "",
                *code(goal["required_terms"]),
                "",
                "开场正式报价参数：",
                "",
                *code(case["turns"][0].get("offer")),
                "",
                "#### 完整公开对话",
                "",
            ]
            turn = 0
            for row in run["stream"]:
                kind = row["kind"]
                if kind == "BUYER_INPUT":
                    turn += 1
                    speaker = f"第 {turn} 轮买家"
                elif kind == "PUBLIC_REPLY":
                    speaker = f"第 {turn} 轮卖家"
                elif kind == "APPROVAL_NOTIFICATION":
                    speaker = "审批通知"
                else:
                    continue
                evidence_counts["public_events"] += 1
                lines += [f"**{speaker}，公开序号 {row['seq']}**", ""]
                lines += quote(row["payload"].get("content", "")) + [""]
                if kind == "PUBLIC_REPLY":
                    markers = {
                        k: row["payload"][k]
                        for k in ("outcome", "stop_reason")
                        if k in row["payload"]
                    }
                    if markers:
                        lines += ["记录状态：" + json.dumps(markers, ensure_ascii=False), ""]
            lines += ["#### 内部候选和工具执行记录", ""]
            turn = 0
            for row in run["stream"]:
                kind = row["kind"]
                if kind == "BUYER_INPUT":
                    turn += 1
                    continue
                if kind == "MODEL_CANDIDATE":
                    evidence_counts["model_candidates"] += 1
                    lines += [f"**第 {turn} 轮，序号 {row['seq']}，模型草稿**", ""]
                    lines += quote(msg_data(row).get("content", "")) + [""]
                elif kind == "TOOL_CALL":
                    evidence_counts["tool_call_events"] += 1
                    origin = (
                        "结构化收尾适配"
                        if row["payload"].get("tool_call_origin") == "STRUCTURED_FINISH"
                        else "模型工具调用"
                    )
                    lines += [f"**第 {turn} 轮，序号 {row['seq']}，{origin}**", ""]
                    data = msg_data(row)
                    if data.get("content"):
                        lines += ["模型附带文本：", "", *quote(data["content"]), ""]
                    for tool in data.get("tool_calls", []):
                        lines += [
                            f"工具 `{tool['name']}`，调用标识 `{tool['id']}`：",
                            "",
                            *code(tool["args"]),
                            "",
                        ]
                elif kind == "TOOL_RESULT":
                    evidence_counts["tool_results"] += 1
                    data = msg_data(row)
                    lines += [f"**第 {turn} 轮，序号 {row['seq']}，工具反馈**", ""]
                    lines += [
                        f"对应调用：`{data.get('tool_call_id')}`。",
                        "",
                        *code(tool_result(row)),
                        "",
                    ]
                elif kind == "RUNTIME_EVENT":
                    lines += [
                        f"**第 {turn} 轮，序号 {row['seq']}，业务路由事件**",
                        "",
                        *code(row["payload"]),
                        "",
                    ]
            lines += ["#### 审核模型原始回执", ""]
            audit_calls = [
                (n, c) for n, c in calls[run["run_id"]] if c["purpose"] == "REPLY_REVIEW"
            ]
            for audit_number, (call_line, call) in enumerate(audit_calls, 1):
                payload = review_input(call)
                decision = payload["finish"]["decision"]
                evidence_counts["review_calls"] += 1
                lines += [
                    f"**审核 {audit_number}，对应买家输入序号 {payload['current_input_seq']}**",
                    "",
                    f"实际调用：`{call['call_id']}`；调用记录第 {call_line} 行。",
                    "",
                    "被审核的候选回复：",
                    "",
                    *quote(decision.get("reply", "")),
                    "",
                    f"候选动作：`{decision.get('action')}`。",
                    "",
                    "审核模型响应原文：",
                    "",
                    *code(call.get("response", {}).get("data", {}).get("content", "")),
                    "",
                ]
            lines += ["#### 结束时的正式业务承诺", "", *code(run["commitments"]), ""]
            lines += [
                "#### 人工审查记录",
                "",
                "- 审查结论：待填写。",
                "- 问题分类：待填写，可选模型回复、审核误判、事实引用、输出协议、权限设计或其他。",
                "- 修复优先级：待填写。",
                "- 修改建议及依据：待填写。",
                "",
                f"原始运行记录：`{run['source_batch']}`；"
                f"[聚合运行记录第 {source_line} 行]"
                f"({runs_path.resolve().as_posix()}:{source_line})。",
                "",
            ]
    lines += [
        "## 原始证据核对",
        "",
        "公开对话、内部候选、工具参数与反馈以及审核响应均按记录原文导出，"
        "未替换为原因摘要。分类主题用于检索，人工结论尚未填写。",
        "",
        f"[原始运行集合]({runs_path.resolve().as_posix()})",
        "",
        f"[原始模型调用集合]({calls_path.resolve().as_posix()})",
        "",
        "| 原始文件 | SHA-256 |",
        "| --- | --- |",
    ]
    for path in (runs_path, calls_path, root / "scenarios-corrected.json"):
        lines.append(f"| {path.name} | `{hashlib.sha256(path.read_bytes()).hexdigest()}` |")
    lines += ["", "完整评测口径见 [V4 最新评测报告](V4_最新版真实模型动态评测报告.md)。", ""]
    content = "\n".join(lines)
    assert len(re.findall(r"^### F\d{2} ", content, re.MULTILINE)) == 26
    assert evidence_counts["review_calls"] == reviews == 127
    assert evidence_counts["public_events"] == sum(
        s["kind"] in {"BUYER_INPUT", "PUBLIC_REPLY", "APPROVAL_NOTIFICATION"}
        for _, _, r in numbered
        for s in r["stream"]
    )
    for kind, field in (
        ("MODEL_CANDIDATE", "model_candidates"),
        ("TOOL_CALL", "tool_call_events"),
        ("TOOL_RESULT", "tool_results"),
    ):
        assert evidence_counts[field] == sum(
            s["kind"] == kind for _, _, r in numbered for s in r["stream"]
        )
    assert content.count("\n#### 人工审查记录\n") == 26
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "seller_failures": 26,
                "unique_scenarios": 18,
                "evidence": dict(evidence_counts),
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    export(args.root, args.output)


if __name__ == "__main__":
    main()
