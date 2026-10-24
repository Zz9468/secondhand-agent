import os
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.runtime.contracts import utcnow
from app.runtime.database import graph_session_factory
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue
from tests.integration.factories import create_negotiation


class Clock:
    def __init__(self):
        self.now = utcnow()

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += timedelta(seconds=seconds)


@pytest.fixture(scope="session")
def graph_factory():
    if os.getenv("RUN_P0_INTEGRATION") != "1":
        pytest.skip("use python -m scripts.run_graph_tests for isolated real MySQL/PostgreSQL")
    url = os.environ["P0_MYSQL_URL"]
    assert make_url(url).database.startswith("p0_test_")
    env = {**os.environ, "DATABASE_URL": url, "LANGSMITH_TRACING": "false"}
    backend = Path(__file__).resolve().parents[2]
    for revision in ("head", "5991c96c16b8", "head"):
        command = "downgrade" if revision == "5991c96c16b8" else "upgrade"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", command, revision],
            cwd=backend,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    factory = graph_session_factory(url)
    yield factory
    factory.kw["bind"].dispose()


@pytest.fixture
def case(graph_factory):
    sid, buyer = create_negotiation(
        graph_factory, minimum_net_price=Decimal("750"), auto_accept_threshold=Decimal("850")
    )
    clock = Clock()
    queue = InputQueue(graph_factory, clock=clock)
    queue.enroll(sid, buyer)
    runner = GraphRunner(graph_factory, os.environ["P0_POSTGRES_URL"], queue=queue)
    return graph_factory, sid, buyer, clock, queue, runner
