"""Small query/model/transaction/publish graph used for real restart experiments."""

import os
from dataclasses import asdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select

from app.p0.contracts import RunIdentity
from app.p0.store import InjectedFailure, products


class ProbeState(TypedDict, total=False):
    run_id: str
    input_seq: int
    input_hash: str
    config_hash: str
    graph_version: str
    session_id: str
    query: str
    product: str
    model_result: dict
    receipt: dict
    review_count: int
    intents: list[str]
    approved: bool


def new_turn(run):
    return {
        **asdict(RunIdentity(run["id"], run["seq"], run["payload_hash"])),
        "session_id": run["session_id"],
        "query": run["payload"]["text"],
        "product": "",
        "model_result": {},
        "receipt": {},
        "review_count": 0,
        "intents": [],
        "approved": False,
    }


def build_graph(saver, store, owner, *, fault="", approval=False):
    def crash(point):
        if fault == point:
            os._exit(73)  # Real abrupt process exit; no Python cleanup.

    def query(state):
        # Check the initial input has actually reached durable storage before side effects.
        checkpoint = saver.get_tuple({"configurable": {"thread_id": state["session_id"]}})
        assert checkpoint.checkpoint["channel_values"]["run_id"] == state["run_id"]
        with store.engine.connect() as db:
            title = db.execute(
                select(products.c.title).where(products.c.id == state["session_id"])
            ).scalar_one()
        return {"product": title}

    def semantic(state):
        value = store.model_artifact(state["run_id"], *owner, lambda: {"intent": "ASK_PRODUCT"})
        crash("after_model")
        if fault == "semantic_error":
            raise InjectedFailure("semantic error after accepted model artifact")
        return {"model_result": value, "intents": [value["intent"]]}

    def commit(state):
        result = store.command(state["run_id"], *owner)
        crash("after_command")
        return {"receipt": result}

    def wait(state):
        result = interrupt({"run_id": state["run_id"], "question": "P0 approval?"})
        return {"approved": result["approved"]}

    def publish(state):
        store.publish(state["run_id"], *owner)
        crash("after_publish")
        return {"review_count": 1}

    builder = StateGraph(ProbeState)
    builder.add_node("query_product", query)
    builder.add_node("semantic", semantic)
    builder.add_node("commit", commit)
    builder.add_node("publish", publish)
    builder.add_edge(START, "query_product")
    builder.add_edge("query_product", "semantic")
    builder.add_edge("semantic", "commit")
    if approval:
        builder.add_node("approval", wait)
        builder.add_edge("commit", "approval")
        builder.add_edge("approval", "publish")
    else:
        builder.add_edge("commit", "publish")
    builder.add_edge("publish", END)
    return builder.compile(checkpointer=saver)
