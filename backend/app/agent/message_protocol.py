import json
from dataclasses import dataclass, field

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)


def json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class StreamRecord:
    seq: int
    kind: str
    payload: dict
    task_id: int | None = None
    branch_id: str | None = None
    visibility: str = "INTERNAL"

    @classmethod
    def from_row(cls, row):
        return cls(
            row.stream_seq,
            row.kind,
            row.payload,
            row.model_task_id,
            row.run_branch_id,
            row.visibility,
        )


@dataclass
class ContextState:
    summary: str = ""
    covered_through: int = 0
    memory: dict = field(default_factory=dict)
    summary_sources: list[int] = field(default_factory=list)


def encode_message(message: BaseMessage) -> dict:
    return json_value(message_to_dict(message))


def wire_message(message: BaseMessage) -> dict:
    """Provider-visible fields; artifacts and local response metadata are excluded."""
    result = {
        "role": {"ai": "assistant", "human": "user"}.get(message.type, message.type),
        "content": message.content,
    }
    if isinstance(message, AIMessage) and message.tool_calls:
        result["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c["args"], ensure_ascii=False),
                },
            }
            for c in message.tool_calls
        ]
    if isinstance(message, ToolMessage):
        result.update(tool_call_id=message.tool_call_id, name=message.name)
    return result


def decode_message(payload: dict) -> BaseMessage:
    return messages_from_dict([payload])[0]


def record_message(record: StreamRecord) -> BaseMessage | None:
    if record.kind == "BUYER_INPUT":
        return HumanMessage(content=record.payload["content"])
    if record.kind in {"PUBLIC_REPLY", "APPROVAL_NOTIFICATION", "SESSION_NOTIFICATION"}:
        return AIMessage(content=record.payload["content"])
    if record.kind in {"TOOL_CALL", "TOOL_RESULT", "MODEL_CANDIDATE"}:
        return decode_message(record.payload["message"])
    return None


class MessageProtocolValidator:
    @staticmethod
    def validate(messages: list[BaseMessage], *, allow_pending: bool = False) -> None:
        pending: set[str] = set()
        seen: set[str] = set()
        for message in messages:
            if isinstance(message, ToolMessage):
                if message.tool_call_id not in pending:
                    raise ValueError("Orphan or duplicate tool result")
                pending.remove(message.tool_call_id)
                continue
            if pending:
                raise ValueError("Tool calls must receive all results before another message")
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    call_id = call.get("id")
                    if not call_id or call_id in seen:
                        raise ValueError("Tool call IDs must be present and unique")
                    seen.add(call_id)
                    pending.add(call_id)
        if pending and not allow_pending:
            raise ValueError("Incomplete tool exchange")

    @staticmethod
    def groups(records: list[StreamRecord]) -> list[list[StreamRecord]]:
        groups: list[list[StreamRecord]] = []
        pending: set[str] = set()
        for record in records:
            message = record_message(record)
            if message is None:
                continue
            if not pending:
                groups.append([])
            groups[-1].append(record)
            if isinstance(message, AIMessage):
                pending.update(call["id"] for call in message.tool_calls)
            elif isinstance(message, ToolMessage):
                pending.discard(message.tool_call_id)
        return groups
