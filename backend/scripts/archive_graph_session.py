"""Dry-run a terminal session archive; --apply keeps audit/tombstones and removes checkpoints."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from hashlib import file_digest
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.runtime.database import graph_session_factory
from app.services.graph_retention_service import GraphRetentionService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-manifest", type=Path)
    args = parser.parse_args()
    if args.apply:
        if not args.backup_manifest:
            parser.error("--apply requires a verified quiescent backup manifest")
        manifest = json.loads(args.backup_manifest.read_text(encoding="utf-8"))
        if manifest.get("format") != "p6-quiescent-v1":
            parser.error("unsupported backup manifest")
        for name in ("mysql.sql", "checkpoint.dump"):
            with (args.backup_manifest.parent / name).open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() != manifest["sha256"][name]:
                    parser.error("backup checksum mismatch")
    settings = get_settings()
    factory = graph_session_factory(settings.database_url)
    try:
        if args.apply:
            created = datetime.fromisoformat(manifest["created_at"])
            if not timedelta(0) <= datetime.now(UTC) - created <= timedelta(hours=24):
                parser.error("backup must be from the last 24 hours")
            with factory() as db:
                if db.scalar(text("SELECT @@server_uuid")) != manifest["mysql_server_uuid"]:
                    parser.error("backup belongs to a different MySQL instance")
        print(
            json.dumps(
                GraphRetentionService(factory, settings.checkpoint_database_url).archive(
                    args.session_id, dry_run=not args.apply
                )
            )
        )
    finally:
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    main()
