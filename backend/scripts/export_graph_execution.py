"""从官方 checkpoint 历史导出节点位置，不将其伪装为模型 callback/远端 trace。"""

import argparse
import json
from pathlib import Path

from app.core.config import get_settings
from app.db.models import NegotiationSession
from app.graphs.negotiation.semantic_graph import build_semantic_graph
from app.persistence.checkpointer import locked_saver
from app.runtime.contracts import thread_id
from app.runtime.database import graph_session_factory
from app.runtime.input_queue import InputQueue


def explain(factory, postgres_url, sid):
    with factory() as db:
        session = db.get(NegotiationSession, sid)
        if not session or session.graph_version not in {"negotiation-v2", "negotiation-v3"}:
            raise ValueError("unsupported graph session")
        version = session.graph_version
    with locked_saver(postgres_url, thread_id(sid), instance="explain") as (saver, _):
        graph = build_semantic_graph(
            saver, InputQueue(factory), None, quality=version == "negotiation-v3"
        )
        history = list(graph.get_state_history({"configurable": {"thread_id": thread_id(sid)}}))
    return {
        "source": "official_checkpoint_history",
        "session_id": sid,
        "graph_version": version,
        "remote_trace": False,
        "steps": [
            {
                "checkpoint_id": s.config["configurable"]["checkpoint_id"],
                "created_at": s.created_at,
                "step": s.metadata.get("step"),
                "next_nodes": list(s.next),
                "input_seq": s.values.get("input_seq"),
                "published_message_id": s.values.get("published_message_id"),
            }
            for s in reversed(history)
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    factory = graph_session_factory(settings.database_url)
    try:
        result = explain(factory, settings.checkpoint_database_url, args.session_id)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
