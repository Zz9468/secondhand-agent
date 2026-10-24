"""仅供隔离真实数据库故障注入；不被公开 API 或生产 Worker 调用。"""

import json
import os
import sys
from datetime import datetime

from sqlalchemy.engine import make_url

from app.runtime.database import graph_session_factory
from app.runtime.graph_runner import GraphRunner
from app.runtime.input_queue import InputQueue

if __name__ == "__main__":
    assert os.environ.get("RUN_P0_INTEGRATION") == "1"
    url = os.environ["P0_MYSQL_URL"]
    assert make_url(url).database.startswith("p0_test_")
    spec = json.loads(sys.stdin.read())
    factory = graph_session_factory(url)
    queue = InputQueue(factory, clock=lambda: datetime.fromisoformat(spec["now"]))

    def fault(point):
        if point == spec.get("fault"):
            os._exit(73)

    try:
        provider = None
        if "semantic_frames" in spec:
            from tests.graph.test_semantic import FakeSemanticModel

            provider = FakeSemanticModel()
            provider.values = spec["semantic_frames"]
        result = GraphRunner(
            factory,
            os.environ["P0_POSTGRES_URL"],
            queue=queue,
            fault=fault,
            semantic_provider=provider,
        ).run(spec["run_id"])
        print(json.dumps(result))
    finally:
        factory.kw["bind"].dispose()
