import json

from langchain_core.messages import AIMessage, ToolMessage


class ScriptedAgentProvider:
    """Known behavior for verifying the evaluation machinery, not effect claims."""

    def __init__(self):
        self.counter = 0

    def invoke_agent(self, messages, tools, config, *, purpose="REACT"):
        self.counter += 1
        usage = {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}
        if purpose == "SUMMARY":
            payload = json.loads(messages[-1].content)
            seqs = [r["seq"] for r in payload["records"]]
            facts = [
                r.get("payload", r.get("message", {})).get("content", "")
                for r in payload["records"]
                if r["kind"] == "BUYER_INPUT"
            ]
            text = "历史买家诉求：" + "；".join(facts[-6:])
            return AIMessage(
                content=json.dumps({"text": text, "source_seqs": seqs}, ensure_ascii=False),
                usage_metadata=usage,
            )
        if purpose == "MEMORY":
            return AIMessage(content='{"entries":[]}', usage_metadata=usage)
        if purpose == "REPLY_REVIEW":
            return AIMessage(
                content='{"approved":true,"reason":"脚本模拟审核，仅验证执行管线","fact_checks":[]}',
                usage_metadata=usage,
            )
        text = "\n".join(str(m.content) for m in messages if m.type == "human")
        decision = {
            "action": "INQUIRY",
            "reason": "依据公开事实回答咨询",
            "reply": "请查看商品描述，也可以说明您关注的问题。",
            "dialogue_acts": [{"kind": "ASK_FACT", "subject": "PRODUCT_DETAILS"}],
        }
        if "底价" in text or "最低价" in text:
            decision["dialogue_acts"] = [{"kind": "ASK_PRIVATE_INFO", "subject": "PRICE_FLOOR"}]
        if purpose == "CANDIDATE":
            return AIMessage(content=json.dumps(decision, ensure_ascii=False), usage_metadata=usage)
        if config.get("reply_strategy") == "conversation-v1":
            decision.pop("dialogue_acts")
            decision.update(topics=[], needs_clarification=False)
        if purpose == "CONVERSATION_FINISH":
            return AIMessage(
                content=json.dumps({"decision": decision}, ensure_ascii=False), usage_metadata=usage
            )
        if purpose == "REACT_FINISH":
            return AIMessage(
                content=json.dumps({"decision": decision}, ensure_ascii=False), usage_metadata=usage
            )
        if not any(isinstance(m, ToolMessage) for m in messages[-3:]):
            name, args = "get_product_info", {}
        else:
            name, args = "finish_turn", {"decision": decision}
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"script-{self.counter}",
                    "type": "tool_call",
                }
            ],
            usage_metadata=usage,
        )
