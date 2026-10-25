"""Subprocess fault fixture: never imported by a production worker."""

import os
import time
from datetime import datetime

from app.runtime.contracts import BudgetExhausted
from app.runtime.database import graph_session_factory
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue
from app.services.model_call_ledger_service import ModelCallLedgerService


def main():
    factory = graph_session_factory(os.environ["GRAPH_EXECUTION_DATABASE_URL"])
    run = os.environ["GRAPH_EXECUTION_RUN_ID"]
    try:
        if os.getenv("P4_PROCESS_MODE") == "budget":
            now = datetime.fromisoformat(os.environ["P4_PROCESS_NOW"])
            queue = InputQueue(factory, clock=lambda: now)
            owner = queue.claim(run)
            try:
                ModelCallLedgerService(factory, clock=queue.clock).call(
                    run,
                    owner,
                    "restart",
                    lambda: ({"unexpected": True}, 1, None),
                    reserved_tokens=1,
                )
            except BudgetExhausted:
                queue.release(run, owner)
                return 0
            return 2

        def fault(point):
            if point == "after_command":
                while True:
                    time.sleep(1)  # Keep the PostgreSQL connection alive, ignoring model timeout.

        GraphRunner(factory, os.environ["GRAPH_EXECUTION_POSTGRES_URL"], fault=fault).run(run)
        return 3
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    raise SystemExit(main())
