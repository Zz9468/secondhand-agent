import json
from copy import deepcopy

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from app.agent.message_protocol import record_message, wire_message
from app.agent.runtime_state import FinishTurnInput

TERMINAL_TOOLS = {"submit_counter_offer", "accept_offer", "request_approval", "finish_turn"}
READ_TOOLS = {
    "get_product_info",
    "get_negotiation_state",
    "evaluate_offer",
    "search_conversation_history",
}
TOOL_DESCRIPTIONS = {
    "get_product_info": "查询当前会话商品的公开事实，不返回卖家私有价格阈值。",
    "get_negotiation_state": "查询当前协商状态、最近报价和非敏感策略信息。",
    "evaluate_offer": "评估交易条件的可执行权限，不返回卖家的具体价格阈值。",
    "submit_counter_offer": "校验并持久化 Agent 正式还价；审批区和禁止区不会写入。",
    "accept_offer": "只接受本轮正式买家报价，业务层重新验证当前报价与自动授权。",
    "request_approval": "为本轮正式买家报价创建审批请求，申请审批不等于卖家接受。",
}


class SearchHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(max_length=120)
    limit: int = Field(default=6, strict=True, ge=1, le=12)


def search_history(records, args):
    query = SearchHistoryInput.model_validate(args)
    matches = []
    for record in records:
        message = record_message(record)
        if message is None:
            continue
        # Return the original provider-visible message with its source ID.
        # Do not recursively copy prior search artifacts or audit metadata.
        payload = {"message": wire_message(message)}
        if query.query in json.dumps(payload, ensure_ascii=False):
            matches.append({"seq": record.seq, "kind": record.kind, "payload": payload})
    return {"ok": True, "records": matches[-query.limit :]}


def finish_turn_schema(config=None):
    from app.agent.conversation_reply import ConversationFinish, conversational

    if conversational(config or {}):
        return ConversationFinish.model_json_schema()
    schema = FinishTurnInput.model_json_schema()
    decision = schema["$defs"]["NegotiationDecision"]
    decision["properties"] = {
        key: value
        for key, value in decision["properties"].items()
        if key in {"action", "dialogue_acts", "reason", "reply"}
    }
    decision["properties"]["action"] = {"type": "string", "enum": ["INQUIRY", "REJECT"]}
    decision["properties"]["dialogue_acts"]["description"] = (
        "逐项描述本轮买家实际表达的诉求，不能描述 Agent 打算反问或引导的话题。"
        "INQUIRY 必须至少一项，reply 应回答这些诉求；REJECT 必须为空数组。"
    )
    decision["required"] = ["action", "dialogue_acts", "reason", "reply"]
    return schema


def tool_schemas(tools, config=None) -> list[dict]:
    schemas = [convert_to_openai_tool(t) for t in tools]
    schemas.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "search_conversation_history",
                    "description": "检索当前会话原始历史，包含来源序号；关键词为空时读取近期历史。",
                    "parameters": SearchHistoryInput.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish_turn",
                    "description": (
                        "结束咨询、澄清或拒绝，附带有来源的记忆增量。"
                        "dialogue_acts 描述本轮买家诉求，"
                        "后端据此生成可信回复；寒暄使用 GENERAL，商品咨询使用 PRODUCT_DETAILS。"
                    ),
                    "parameters": finish_turn_schema(config),
                },
            },
        ]
    )

    # Qwen's schema parser does not support Decimal's look-ahead regex.
    def normalize(value):
        if isinstance(value, dict):
            value.pop("pattern", None)
            for child in value.values():
                normalize(child)
        elif isinstance(value, list):
            for child in value:
                normalize(child)

    schemas = deepcopy(schemas)
    if (config or {}).get("reply_strategy") == "conversation-v1":
        schemas[-1]["function"]["description"] = (
            "以自然回复结束咨询、澄清或拒绝。基于可信事实直接回应本轮问题；"
            "topics 是可选的自由文本标签，不参与回复路由，不填 dialogue_acts。"
            "普通回复不能代替正式报价、接受或审批工具。"
        )
    for schema in schemas:
        function = schema["function"]
        if function["name"] in TOOL_DESCRIPTIONS:
            function["description"] = TOOL_DESCRIPTIONS[function["name"]]
        if not function["parameters"].get("properties"):
            function["parameters"]["additionalProperties"] = False
    normalize(schemas)
    return schemas


def batch_error(calls: list[dict]) -> str | None:
    names = [call["name"] for call in calls]
    if any(name not in READ_TOOLS | TERMINAL_TOOLS for name in names):
        return "UNKNOWN_TOOL"
    terminal = [i for i, name in enumerate(names) if name in TERMINAL_TOOLS]
    if len(terminal) > 1 or (terminal and terminal[0] != len(calls) - 1):
        return "TERMINAL_TOOL_MUST_BE_UNIQUE_AND_LAST"
    return None


def visible_tool_result(value, token_limit):
    """Preserve full artifacts while bounding provider-visible, structurally valid JSON."""
    visible = deepcopy(value)

    def size():
        return len(json.dumps(visible, ensure_ascii=False, default=str).encode()) // 2

    if size() <= token_limit:
        return visible
    visible["artifact_available"] = True
    if isinstance(visible.get("records"), list):
        while visible["records"] and size() > token_limit:
            visible["records"].pop(0)
        visible["records_omitted"] = len(value["records"]) - len(visible["records"])
    if size() > token_limit and isinstance(visible.get("product"), dict):
        visible["product"].pop("description", None)
        visible["description_in_current_facts"] = True
    if size() > token_limit:
        essential = {
            k: visible[k] for k in ("ok", "offer", "approval", "authorization") if k in visible
        }
        if "error" in visible:
            essential["error"] = {"code": visible["error"].get("code", "TOOL_ERROR")}
        essential["artifact_available"] = True
        visible = essential
    return visible
