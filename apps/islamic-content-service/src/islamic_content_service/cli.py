from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .db import Database
from .main import build_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Islamic content service CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command", required=True)

    bootstrap_parser = sync_subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--sources", default="quran,hadith")

    sync_subparsers.add_parser("quran")
    sync_subparsers.add_parser("hadith")

    db_parser = subparsers.add_parser("db")
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    db_subparsers.add_parser("migrate")
    db_subparsers.add_parser("summary")

    return parser.parse_args()


async def _run() -> int:
    args = parse_args()
    settings = Settings()
    if args.command == "db":
        db = Database(settings.database)
        await db.connect()
        try:
            if args.db_command == "migrate":
                await db.migrate(Path(__file__).resolve().parents[2] / "migrations" / "001_init_schema.sql")
                print("migration applied")
            elif args.db_command == "summary":
                runtime = await build_runtime(settings)
                try:
                    print(json.dumps(await runtime.bootstrap_service.get_canonical_summary(), ensure_ascii=False, indent=2))
                finally:
                    await runtime.close()
            return 0
        finally:
            await db.close()

    runtime = await build_runtime(settings)
    try:
        if args.sync_command == "bootstrap":
            sources = [item.strip() for item in args.sources.split(",") if item.strip()]
            print(json.dumps(await runtime.bootstrap_service.bootstrap(sources), ensure_ascii=False, indent=2))
        elif args.sync_command == "quran":
            print(json.dumps(await runtime.bootstrap_service.bootstrap(["quran"]), ensure_ascii=False, indent=2))
        elif args.sync_command == "hadith":
            print(json.dumps(await runtime.bootstrap_service.bootstrap(["hadith"]), ensure_ascii=False, indent=2))
        return 0
    finally:
        await runtime.close()


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
