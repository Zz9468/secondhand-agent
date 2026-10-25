from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.runtime.contracts import Conflict


class ApprovalState(TypedDict):
    approval_id: int
    graph_version: str
    event_id: str | None
    result: dict | None
    public_message_id: int | None


def build_approval_graph(saver, commands, owner, publish, fault=lambda point: None):
    def load(state):
        event = commands.terminal_event(state["approval_id"])
        fault("after_load")
        return {"event_id": event}

    def wait(state):
        # No side effects before interrupt: this node restarts on resume.
        reference = interrupt({"approval_id": state["approval_id"], "kind": "SELLER_DECISION"})
        if not isinstance(reference, dict) or set(reference) != {"event_id"}:
            raise Conflict("恢复参数必须为审批事件引用")
        return {"event_id": reference["event_id"]}

    def apply(state):
        result = commands.apply(state["approval_id"], state["event_id"], owner)
        fault("after_apply")
        return {"result": result}

    def notice(state):
        source = f"approval:{state['approval_id']}:result:{state['result']['effect_revision']}"
        message = publish(source)
        fault("after_publish")
        return {"public_message_id": message}

    graph = StateGraph(ApprovalState)
    graph.add_node("load_approval", load)
    graph.add_node("wait_for_seller", wait)
    graph.add_node("apply_result", apply)
    graph.add_node("publish_notice", notice)
    graph.add_edge(START, "load_approval")
    graph.add_conditional_edges(
        "load_approval", lambda s: "apply_result" if s["event_id"] else "wait_for_seller"
    )
    graph.add_edge("wait_for_seller", "apply_result")
    graph.add_edge("apply_result", "publish_notice")
    graph.add_edge("publish_notice", END)
    return graph.compile(checkpointer=saver)
