"""Command-line surface of the single migration authority.

    python -m database.authority migrate       # the only sanctioned writer
    python -m database.authority init-fresh    # empty DB -> frozen baseline
    python -m database.authority status        # read-only
    python -m database.authority verify        # read-only fingerprints
    python -m database.authority startup-check # app boot gate (read-only)
    python -m database.authority fingerprint   # print fingerprints (read-only)

The DSN follows the fail-closed rule of database/cli.py: DATABASE_URL or
GATEWAY_DATABASE__DSN, no default password, never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .commands import (
    command_init_fresh,
    command_migrate,
    command_startup_check,
    command_status,
    command_verify,
    default_paths,
    load_baseline,
)
from .constants import DEFAULT_BASELINE_ID
from .runner import MigrationAuthority, role_prefix_from_env


def get_dsn() -> str:
    """DSN from environment only; fail closed, never print it."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("GATEWAY_DATABASE__DSN")
    if dsn:
        return dsn
    try:
        from src.config.settings import Settings

        settings = Settings()
        if getattr(settings, "database", None) and settings.database.dsn:
            return settings.database.dsn
    except Exception:  # noqa: BLE001 - fall through to the fail-closed message
        pass
    print(
        "DATABASE_URL and GATEWAY_DATABASE__DSN are not set and Settings "
        "provided no DSN; refusing to guess a database connection.",
        file=sys.stderr,
    )
    sys.exit(2)


async def _fingerprint_command(authority: MigrationAuthority, baseline_id: str) -> int:
    from .fingerprint import compute_fingerprints

    paths = authority.paths
    baseline, _sha = load_baseline(paths, baseline_id)
    conn = await authority.connect(read_only=True)
    try:
        computed = await compute_fingerprints(
            conn, role_prefix=authority.role_prefix, reference_sets=baseline.reference_data
        )
    finally:
        await conn.close()
    for name in ("structural", "acl", "extensions", "reference_data"):
        print(f"{name}: {computed[name]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="database.authority")
    parser.add_argument(
        "command",
        choices=(
            "migrate",
            "init-fresh",
            "status",
            "verify",
            "startup-check",
            "fingerprint",
        ),
    )
    parser.add_argument("--baseline", default=DEFAULT_BASELINE_ID)
    parser.add_argument(
        "--no-adoption",
        action="store_true",
        help="migrate only the legacy chain; never write the adoption marker",
    )
    args = parser.parse_args(argv)

    dsn = get_dsn()
    paths = default_paths()
    authority = MigrationAuthority(dsn, paths, role_prefix=role_prefix_from_env())

    if args.command == "migrate":
        return asyncio.run(
            command_migrate(
                authority,
                baseline_id=args.baseline,
                allow_adoption=not args.no_adoption,
            )
        )
    if args.command == "init-fresh":
        return asyncio.run(command_init_fresh(authority, baseline_id=args.baseline))
    if args.command == "status":
        return asyncio.run(command_status(authority))
    if args.command == "verify":
        return asyncio.run(command_verify(authority, baseline_id=args.baseline))
    if args.command == "startup-check":
        return asyncio.run(command_startup_check(authority))
    return asyncio.run(_fingerprint_command(authority, args.baseline))


if __name__ == "__main__":
    sys.exit(main())
