"""独立受限清理组件，不接受外部 PID/数据库命令。"""

import argparse
import json
import os
import time

from dotenv import dotenv_values

from app.core.config import PROJECT_ROOT, get_settings
from app.runtime.database import graph_session_factory
from app.runtime.operations import instance_id, pulse, sweep


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    factory = graph_session_factory(get_settings().database_url)
    cleaner_url = os.getenv("CHECKPOINT_CLEANER_URL") or dotenv_values(PROJECT_ROOT / ".env").get(
        "CHECKPOINT_CLEANER_URL"
    )
    if not cleaner_url:
        raise RuntimeError("restricted checkpoint cleaner is not configured")
    identity = instance_id()
    try:
        while True:
            result = sweep(factory, cleaner_url)
            pulse(factory, "watchdog", identity, "error" if result["failed"] else "ok")
            if args.once or any(result.values()):
                print(json.dumps({"service": "watchdog", **result}), flush=True)
            if args.once:
                return int(bool(result["failed"]))
            time.sleep(1)
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    raise SystemExit(main())
