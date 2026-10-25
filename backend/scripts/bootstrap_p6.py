"""生成独立部署配置，保留已有值；可选按白名单导入模型配置。"""

import argparse
import json
import secrets
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
IMPORT_KEYS = (
    "MODEL_API_KEY",
    "MODEL_BASE_URL",
    "MODEL_NAME",
    "MODEL_INPUT_PRICE_PER_MILLION",
    "MODEL_OUTPUT_PRICE_PER_MILLION",
    "MODEL_CACHED_INPUT_PRICE_PER_MILLION",
    "MODEL_COST_CURRENCY",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
)


def prepare(target, source=None):
    current = dict(dotenv_values(target, interpolate=False)) if target.exists() else {}
    defaults = dict(dotenv_values(ROOT / ".env.example", interpolate=False))
    # Generated secrets are independent of an optional source environment.
    for key in (
        "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        "POSTGRES_PASSWORD",
        "CHECKPOINT_PASSWORD",
        "GRAPH_CLEANER_PASSWORD",
        "AUTH_SECRET",
        "DEMO_SELLER_PASSWORD",
    ):
        defaults[key] = secrets.token_hex(32)
    if source:
        imported = dotenv_values(source, interpolate=False)
        defaults.update({k: imported[k] for k in IMPORT_KEYS if imported.get(k)})
    values = {**defaults, **{k: v for k, v in current.items() if v and "CHANGE_ME" not in v}}
    mysql = f"mysql+pymysql://{values['MYSQL_USER']}:{quote(values['MYSQL_PASSWORD'], safe='')}"
    pg = f"postgresql://checkpoint_worker:{quote(values['CHECKPOINT_PASSWORD'], safe='')}"
    cleaner = f"postgresql://graph_cleaner:{quote(values['GRAPH_CLEANER_PASSWORD'], safe='')}"
    local_pg = f"127.0.0.1:{values['POSTGRES_PORT']}/{values['POSTGRES_DB']}"
    urls = {
        "DATABASE_URL": f"{mysql}@127.0.0.1:{values['MYSQL_PORT']}/{values['MYSQL_DATABASE']}"
        "?charset=utf8mb4",
        "CONTAINER_DATABASE_URL": f"{mysql}@mysql:3306/{values['MYSQL_DATABASE']}?charset=utf8mb4",
        "CHECKPOINT_DATABASE_URL": f"{pg}@{local_pg}",
        "CONTAINER_CHECKPOINT_DATABASE_URL": f"{pg}@checkpoint:5432/{values['POSTGRES_DB']}",
        "CHECKPOINT_CLEANER_URL": f"{cleaner}@{local_pg}",
        "CONTAINER_CHECKPOINT_CLEANER_URL": f"{cleaner}@checkpoint:5432/{values['POSTGRES_DB']}",
    }
    for key, value in urls.items():
        if not current.get(key) or "CHANGE_ME" in current[key]:
            values[key] = value
    updates = {k: v for k, v in values.items() if v is not None and current.get(k) != v}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write("\n# P6 independent deployment configuration\n")
        for key, value in updates.items():
            stream.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
    return sorted(updates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    parser.add_argument("--source-env", type=Path)
    args = parser.parse_args()
    fields = prepare(args.output, args.source_env)
    print(json.dumps({"updated_fields": fields, "secrets_printed": False}))


if __name__ == "__main__":
    main()
