#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.config.settings import Settings
from src.persistence.database import DatabaseStorage
from src.persistence.islamic_content_repository import IslamicContentRepository
from src.services.islamic_content import IslamicContentService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and normalize Islamic content for third-party APIs."
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override cache dir (defaults to GATEWAY_ISLAMIC_CONTENT__CACHE_DIR or settings default).",
    )
    parser.add_argument("--skip-quran", action="store_true")
    parser.add_argument("--include-hadith", action="store_true")
    parser.add_argument("--skip-duas", action="store_true")
    parser.add_argument(
        "--hadith-collections",
        nargs="*",
        default=None,
        help="Optional subset of hadith collections to sync, e.g. bukhari muslim",
    )
    parser.add_argument(
        "--persist-db",
        action="store_true",
        help="Persist normalized canonical data into PostgreSQL after fetch.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = Settings()
    islamic_settings = settings.islamic_content.model_copy(deep=True)
    if args.output_dir:
        islamic_settings.cache_dir = args.output_dir

    database = None
    repository = None
    if args.persist_db:
        if not settings.database.enabled:
            raise SystemExit("Database persistence requested but GATEWAY_DATABASE__ENABLED is false.")
        database = DatabaseStorage(
            dsn=settings.database.dsn,
            enabled=settings.database.enabled,
            auto_init=True,
            permission_cache_ttl_seconds=settings.database.permission_cache_ttl_seconds,
            pool_min_size=settings.database.pool_min_size,
            pool_max_size=settings.database.pool_max_size,
            api_key_usage_flush_interval_seconds=settings.database.api_key_usage_flush_interval_seconds,
            api_key_usage_flush_batch_size=settings.database.api_key_usage_flush_batch_size,
        )
        await database.connect()
        repository = IslamicContentRepository(database)

    service = IslamicContentService(islamic_settings, repository=repository)
    try:
        manifest = await service.sync_static_content(
            include_quran=not args.skip_quran,
            include_hadith=args.include_hadith,
            hadith_collections=args.hadith_collections,
            include_duas=not args.skip_duas,
            persist_db=args.persist_db,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        await service.close()
        if database is not None:
            await database.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
