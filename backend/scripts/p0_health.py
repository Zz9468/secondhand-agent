"""Read-only readiness of the isolated P0 environment; no model invocation."""

import json
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]


def main():
    config = dotenv_values(ROOT / ".env")
    result = {
        "worker": "P0 probe processes only; production graph runner pending P1",
        "model_configured": bool(config.get("MODEL_API_KEY") and config.get("MODEL_BASE_URL")),
        "langsmith_configured": bool(config.get("LANGSMITH_API_KEY")),
    }
    engine = create_engine(config["DATABASE_URL"], connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as db:
            result["business_database"] = db.execute(text("SELECT DATABASE()")).scalar_one()
            result["mysql_version"] = db.execute(text("SELECT VERSION()")).scalar_one()
        with psycopg.connect(config["CHECKPOINT_DATABASE_URL"], connect_timeout=3) as db:
            result["checkpoint_database"] = db.execute("SELECT current_database()").fetchone()[0]
            result["postgres_version"] = db.execute("SHOW server_version").fetchone()[0]
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False))
    return int("error_type" in result)


if __name__ == "__main__":
    raise SystemExit(main())
