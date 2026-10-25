"""就绪探测不调用模型、不泄露连接串。"""

import psycopg
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url

from app.runtime.database import graph_session_factory
from app.runtime.operations import service_health


def validate_targets(settings):
    mysql = make_url(settings.database_url)
    postgres = conninfo_to_dict(settings.checkpoint_database_url or "")
    if (
        mysql.username != "langgraph_app"
        or mysql.database != "secondhand_langgraph"
        or mysql.host not in {"mysql", "127.0.0.1", "localhost"}
        or mysql.port not in {3306, 3307}
        or postgres.get("user") != "checkpoint_worker"
        or postgres.get("dbname") != "secondhand_checkpoints"
        or postgres.get("host") not in {"checkpoint", "127.0.0.1", "localhost"}
        or postgres.get("port") not in {"5432", "5433"}
    ):
        raise ValueError("P6 requires the independent LangGraph database targets")


def graph_readiness(settings):
    result = {"checkpoint": "unavailable", "worker": "unavailable", "watchdog": "unavailable"}
    try:
        with psycopg.connect(
            settings.checkpoint_database_url, connect_timeout=2, options="-c statement_timeout=2000"
        ) as db:
            db.execute("SELECT 1 FROM checkpoints LIMIT 1")
            result["checkpoint"] = "ok"
    except Exception:
        pass
    factory = graph_session_factory(settings.database_url)
    try:
        result.update(service_health(factory))
    except Exception:
        pass
    finally:
        factory.kw["bind"].dispose()
    return result
