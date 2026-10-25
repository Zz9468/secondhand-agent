"""停写后的双库备份，默认拒绝在应用运行时备份；不物理删除审计数据。"""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def backup(command, output):
    running = subprocess.check_output(
        [*command, "ps", "--services", "--status", "running"], cwd=ROOT, text=True
    ).splitlines()
    if set(running) & {"api", "worker", "watchdog"}:
        raise RuntimeError("Stop api, worker and watchdog before a consistent two-store backup")
    output.mkdir(parents=True, exist_ok=False)
    server_uuid = subprocess.check_output(
        [
            *command,
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B -e "SELECT @@server_uuid"',
        ],
        cwd=ROOT,
        text=True,
    ).strip()
    specs = {
        "mysql.sql": (
            "mysql",
            [
                "sh",
                "-c",
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot '
                "--single-transaction --routines --triggers --no-tablespaces "
                '--set-gtid-purged=OFF "$MYSQL_DATABASE"',
            ],
        ),
        "checkpoint.dump": (
            "checkpoint",
            [
                "sh",
                "-c",
                'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                "--format=custom --no-owner --no-acl",
            ],
        ),
    }
    hashes = {}
    for name, (service, args) in specs.items():
        path = output / name
        with path.open("xb") as stream:
            result = subprocess.run(
                [*command, "exec", "-T", service, *args],
                cwd=ROOT,
                stdout=stream,
                stderr=subprocess.PIPE,
            )
        if result.returncode:
            raise RuntimeError(f"{service} backup failed; incomplete files retained")
        with path.open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "format": "p6-quiescent-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "mysql_server_uuid": server_uuid,
        "sha256": hashes,
        "audit_retained": True,
        "configuration_secrets_included": False,
        "contains_private_business_data": True,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(backup(["docker", "compose"], args.output)))


if __name__ == "__main__":
    main()
