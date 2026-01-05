"""
Reset admin password in the database.

Usage:
  python scripts/reset_admin_password.py --password "123456.dc"

If --password is omitted, you will be prompted securely.
DSN is read from GATEWAY_DATABASE__DSN or defaults to the local dev DSN.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    import asyncpg
except Exception as exc:  # pragma: no cover - runtime dependency
    raise SystemExit("asyncpg is required. Install with: pip install asyncpg") from exc

try:
    import bcrypt
except Exception as exc:  # pragma: no cover - runtime dependency
    raise SystemExit("bcrypt is required. Install with: pip install bcrypt") from exc


DEFAULT_DSN = "postgresql://postgres:111111@127.0.0.1:5433/gateway"


async def main() -> None:
    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser(description="Reset admin password")
    parser.add_argument("--password", help="New admin password")
    parser.add_argument("--dsn", help="PostgreSQL DSN")
    args = parser.parse_args()

    password = args.password or getpass.getpass("New admin password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")

    dsn = args.dsn or os.getenv("GATEWAY_DATABASE__DSN", DEFAULT_DSN)

    conn = await asyncpg.connect(dsn)
    try:
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
        result = await conn.execute(
            """
            UPDATE users
            SET password_hash = $1,
                force_password_change = FALSE,
                password_changed_at = NOW(),
                login_attempts = 0,
                locked_until = NULL,
                updated_at = NOW()
            WHERE user_id = 'admin'
            """,
            hashed,
        )
        print(f"Reset completed: {result}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
