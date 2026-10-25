"""Dry-run a terminal session archive; --apply keeps audit/tombstones and removes checkpoints."""

import argparse
import json

from app.core.config import get_settings
from app.runtime.database import graph_session_factory
from app.services.graph_retention_service import GraphRetentionService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    factory = graph_session_factory(settings.database_url)
    try:
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
