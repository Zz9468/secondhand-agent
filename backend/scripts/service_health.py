"""容器健康检查：仅报告服务名及状态，不打印连接配置。"""

import sys

from app.core.config import get_settings
from app.runtime.database import graph_session_factory
from app.runtime.operations import service_health


def main():
    factory = graph_session_factory(get_settings().database_url)
    try:
        status = service_health(factory).get(sys.argv[1], "unavailable")
        print(status)
        return int(status != "ok")
    except Exception:
        print("unavailable")
        return 1
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    raise SystemExit(main())
