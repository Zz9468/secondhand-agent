"""Disposable execution process; all credentials arrive through inherited environment."""

import os

from app.runtime.database import graph_session_factory
from app.runtime.graph_runner import GraphRunner


def main():
    factory = graph_session_factory(os.environ["GRAPH_EXECUTION_DATABASE_URL"])
    try:
        result = GraphRunner(factory, os.environ["GRAPH_EXECUTION_POSTGRES_URL"]).run(
            os.environ["GRAPH_EXECUTION_RUN_ID"]
        )
        return 0 if result else 2
    except Exception:
        return 1  # Error bodies must never expose SDK requests or credentials on stderr.
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    raise SystemExit(main())
