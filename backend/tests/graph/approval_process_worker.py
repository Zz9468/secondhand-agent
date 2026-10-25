"""P3 real process failure injection in disposable databases only."""

import json
import os
import sys
from datetime import datetime

from sqlalchemy.engine import make_url

from app.runtime.approval_runner import ApprovalRunner
from app.runtime.database import graph_session_factory

if __name__ == "__main__":
    assert os.environ.get("RUN_P0_INTEGRATION") == "1"
    url = os.environ["P0_MYSQL_URL"]
    assert make_url(url).database.startswith("p0_test_")
    spec = json.loads(sys.stdin.read())
    factory = graph_session_factory(url)

    def fault(point):
        if point == spec.get("fault"):
            os._exit(73)

    try:
        result = ApprovalRunner(
            factory,
            os.environ["P0_POSTGRES_URL"],
            clock=lambda: datetime.fromisoformat(spec["now"]),
            fault=fault,
        ).run(spec["approval_id"])
        print(json.dumps(result))
    finally:
        factory.kw["bind"].dispose()
