"""Fresh-process graph runner used only by the P0 integration suite."""

import json
import os
import sys

from langgraph.types import Command
from sqlalchemy import select

from app.p0.contracts import Entry, RunIdentity, choose_entry
from app.p0.graph import build_graph, new_turn
from app.p0.persistence import locked_saver
from app.p0.store import Store, artifacts, messages, receipts


def main():
    spec = json.loads(sys.stdin.read())
    store = Store(os.environ["P0_MYSQL_URL"])
    run = store.read_run(spec["run_id"])
    config = {"configurable": {"thread_id": run["session_id"]}}
    try:
        with locked_saver(os.environ["P0_POSTGRES_URL"], run["session_id"]) as (saver, _):
            owner = store.claim(run["id"])
            if not owner:
                raise RuntimeError("input not claimable")
            if spec.get("fault") == "before_graph":
                os._exit(73)
            graph = build_graph(
                saver,
                store,
                owner,
                fault=spec.get("fault", ""),
                approval=spec.get("approval", False),
            )
            snapshot = graph.get_state(config)
            with store.engine.connect() as db:
                evidence = any(
                    db.execute(
                        select(table).where(
                            (table.c.run_id if table is artifacts else table.c.source) == run["id"]
                        )
                    ).first()
                    for table in (artifacts, receipts, messages)
                )
            entry = choose_entry(
                RunIdentity(run["id"], run["seq"], run["payload_hash"]),
                snapshot.values,
                snapshot.next,
                has_evidence=evidence,
                is_head=True,
            )
            if entry == Entry.HISTORY:
                print(json.dumps({"entry": entry}))
                return
            if entry != Entry.FINALIZE:
                value = new_turn(run) if entry == Entry.START else None
                if "resume" in spec:
                    value = Command(resume=spec["resume"])
                graph.invoke(value, config, durability="sync")
            if spec.get("fault") == "after_end":
                os._exit(73)
            snapshot = graph.get_state(config)
            if not snapshot.next:
                store.finalize(run["id"], *owner, snapshot.values, snapshot.next)
            else:
                store.revoke(run["id"])
            print(
                json.dumps(
                    {"entry": entry, "pending": list(snapshot.next), "values": snapshot.values}
                )
            )
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
