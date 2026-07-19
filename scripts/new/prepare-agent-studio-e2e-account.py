#!/usr/bin/env python3
"""Create a disposable local Assistant isolation account without logging secrets."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shlex
import uuid
from pathlib import Path

import asyncpg
from dotenv import dotenv_values

from src.core.auth.password import hash_password

ROOT = Path(__file__).resolve().parents[2]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _required(values: dict[str, str | None], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"local environment is missing {name}")
    return value


async def _prepare(args: argparse.Namespace) -> None:
    values = dict(dotenv_values(args.env))
    suffix = uuid.uuid4().hex[:12]
    user_id = f"agent-studio-isolation-{suffix}"
    domain = str(values.get("AUTH_ALLOWED_EMAIL_DOMAIN") or "example.com").strip()
    email = f"{user_id}@{domain}"
    password = secrets.token_urlsafe(32)
    password_hash = hash_password(password)
    conn = await asyncpg.connect(
        host="127.0.0.1",
        port=int(_required(values, "POSTGRES_PORT")),
        user=_required(values, "POSTGRES_USER"),
        password=_required(values, "POSTGRES_PASSWORD"),
        database=_required(values, "POSTGRES_DB"),
    )
    try:
        await conn.execute(
            """
            INSERT INTO users (
                user_id, username, email, display_name, tenant_id, tier,
                roles, permissions, status, password_hash,
                force_password_change, email_verified, created_by
            ) VALUES (
                $1, $1, $2, 'Agent Studio Isolation', 'default', 'normal',
                ARRAY['user']::varchar(50)[],
                ARRAY[
                    'console:dashboard:view',
                    'conversation:playground:access',
                    'conversation:thread:create'
                ]::varchar(100)[],
                'active', $3, FALSE, TRUE, 'agent-studio-e2e'
            )
            """,
            user_id,
            email,
            password_hash,
        )
        await conn.execute(
            """
            INSERT INTO user_roles (user_id, role_name, granted_by)
            VALUES ($1, 'user', 'agent-studio-e2e')
            ON CONFLICT (user_id, role_name) DO NOTHING
            """,
            user_id,
        )
    finally:
        await conn.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"ASSISTANT_ISOLATION_EMAIL={shlex.quote(email)}\n")
        handle.write(f"ASSISTANT_ISOLATION_PASSWORD={shlex.quote(password)}\n")
        handle.write("ASSISTANT_ISOLATION_MODEL=qwen3.7-plus\n")
        handle.write("GATEWAY_BASE_URL=http://127.0.0.1:8080\n")
    print(f"Prepared local isolation account; credentials written to {args.output}")


def main() -> None:
    args = _parse_args()
    asyncio.run(_prepare(args))


if __name__ == "__main__":
    main()
