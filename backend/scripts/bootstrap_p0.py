"""Create independent local configuration; never execute or print source secrets."""

import argparse
import json
import secrets
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
IMPORT_FIELDS = {
    "MODEL_API_KEY": "MODEL_API_KEY",
    "MODEL_BASE_URL": "MODEL_BASE_URL",
    "OBSERVABILITY_LANGSMITH_API_KEY": "LANGSMITH_API_KEY",
    "OBSERVABILITY_LANGSMITH_ENDPOINT": "LANGSMITH_ENDPOINT",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, required=True)
    args = parser.parse_args()
    target = ROOT / ".env"
    subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT, check=True)
    current = dotenv_values(target, interpolate=False) if target.exists() else {}
    source = dotenv_values(args.source_env, interpolate=False)
    updates = {}
    for old, new in IMPORT_FIELDS.items():
        if current.get(new):
            status = "preserved"
        elif source.get(old):
            updates[new] = source[old]
            status = "imported"
        else:
            status = "missing"
        print(f"{new}: {status}")
    defaults = {
        "COMPOSE_PROJECT_NAME": "secondhand-agent-langgraph",
        "MYSQL_DATABASE": "secondhand_langgraph",
        "MYSQL_USER": "langgraph_app",
        "MYSQL_PASSWORD": secrets.token_hex(24),
        "MYSQL_ROOT_PASSWORD": secrets.token_hex(24),
        "MYSQL_PORT": "3307",
        "POSTGRES_DB": "secondhand_checkpoints",
        "POSTGRES_USER": "checkpoint_admin",
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "CHECKPOINT_PASSWORD": secrets.token_hex(24),
        "POSTGRES_PORT": "5433",
        "API_PORT": "8100",
        "FRONTEND_PORT": "5174",
        "CORS_ORIGINS": '["http://localhost:5174","http://127.0.0.1:5174"]',
        "AUTH_SECRET": secrets.token_hex(32),
        "MODEL_NAME": "qwen3.7-flash",
        "MODEL_ENABLE_THINKING": "false",
        "MODEL_MAX_RETRIES": "0",
        "LANGSMITH_PROJECT": "secondhand-agent-langgraph-test",
        "LANGSMITH_TRACING": "false",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OBSERVABILITY_LANGSMITH_ENABLED": "false",
    }
    for key, value in defaults.items():
        if not current.get(key) and key not in updates:
            updates[key] = value
    config = {**current, **updates}
    password = config["MYSQL_PASSWORD"]
    mysql = f"mysql+pymysql://{config['MYSQL_USER']}:{password}"
    database = config["MYSQL_DATABASE"]
    urls = {
        "DATABASE_URL": f"{mysql}@127.0.0.1:{config['MYSQL_PORT']}/{database}?charset=utf8mb4",
        "CONTAINER_DATABASE_URL": f"{mysql}@mysql:3306/{database}?charset=utf8mb4",
        "CHECKPOINT_DATABASE_URL": (
            f"postgresql://checkpoint_worker:{config['CHECKPOINT_PASSWORD']}"
            f"@127.0.0.1:{config['POSTGRES_PORT']}/{config['POSTGRES_DB']}"
        ),
    }
    for key, value in urls.items():
        if not current.get(key):
            updates[key] = value
    # JSON-style double quoting is understood by dotenv; no shell evaluation occurs.
    with target.open("a", encoding="utf-8") as stream:
        stream.write("\n# P0 independent configuration\n")
        for key, value in updates.items():
            stream.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
    print("Independent .env ready; no source database/auth/runtime settings imported.")


if __name__ == "__main__":
    main()
