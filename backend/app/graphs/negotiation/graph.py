"""P1：引用输入 → 确定性授权 → 原子动作 → 唯一公开结果。"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.db.models import NegotiationSession, SellerPolicy
from app.runtime.contracts import Conflict, TypedInput
from app.runtime.input_queue import lock_owner
from app.services.business_command_service import BUSINESS_ERRORS, BusinessCommandService
from app.services.graph_business_operations import authorized
from app.services.notification_service import NotificationService


class TurnState(TypedDict):
    run_id: str
    session_id: int
    input_seq: int
    input_hash: str
    config_hash: str
    graph_version: str
    expected_version: int
    authorization: str
    business_result: dict
    published_message_id: int | None
    stop_reason: str | None


def new_turn(run):
    return {
        **{
            key: run[key]
            for key in (
                "session_id",
                "input_seq",
                "input_hash",
                "config_hash",
                "graph_version",
            )
        },
        "run_id": run["id"],
        "expected_version": 0,
        "authorization": "",
        "business_result": {},
        "published_message_id": None,
        "stop_reason": None,
    }


def build_graph(saver, queue, owner, *, fault=lambda point: None):
    commands = BusinessCommandService(queue.factory, clock=queue.clock)
    notifications = NotificationService(queue.factory, clock=queue.clock)

    def authorize(state):
        from app.runtime.contracts import thread_id

        checkpoint = saver.get_tuple(
            {"configurable": {"thread_id": thread_id(state["session_id"])}}
        )
        if (
            not checkpoint
            or checkpoint.checkpoint["channel_values"].get("run_id") != state["run_id"]
        ):
            raise Conflict("初始 checkpoint 尚未持久化")
        with queue.factory() as db, db.begin():
            run, _ = lock_owner(db, state["run_id"], owner, queue.clock())
            run.checkpoint_ref = checkpoint.checkpoint["id"]
            session = db.get(NegotiationSession, run.session_id)
            policy = db.scalar(
                select(SellerPolicy).where(SellerPolicy.product_id == session.product_id)
            )
            payload = TypedInput.model_validate(run.payload)
            authorization = "NO_PRICE_ACTION"
            try:
                if payload.terms:
                    authorization = authorized(payload.terms, policy).reason_code
            except BUSINESS_ERRORS:
                authorization = "INVALID_TERMS"
            # 只返回授权类别；私有策略、成本和展示凭据均不进入图状态。
            return {"expected_version": session.version, "authorization": authorization}

    def commit(state):
        run = queue.read(state["run_id"])
        try:
            if run["payload"]["kind"] == "QUERY":
                result = commands.abort_or_query(state["run_id"], owner)
            else:
                result = commands.execute(state["run_id"], owner, state["expected_version"])
        except BUSINESS_ERRORS as exc:
            # 所有权失效不会被转为可发布结果，abort_or_query 会再次校验所有权。
            result = commands.abort_or_query(state["run_id"], owner, reason=str(exc))
        fault("after_command")
        return {"business_result": result}

    def publish(state):
        # P1 不生成自然文案，不消耗模型预算；同源发布支持独立通知重放。
        message_id = notifications.publish(state["run_id"], owner=owner)
        fault("after_publish")
        return {"published_message_id": message_id}

    graph = StateGraph(TurnState)
    graph.add_node("authorize", authorize)
    graph.add_node("commit_business", commit)
    graph.add_node("publish_reply", publish)
    graph.add_edge(START, "authorize")
    graph.add_edge("authorize", "commit_business")
    graph.add_edge("commit_business", "publish_reply")
    graph.add_edge("publish_reply", END)
    return graph.compile(checkpointer=saver)
