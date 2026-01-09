# -*- coding: utf-8 -*-
"""Full authentication and API diagnosis"""
import asyncio
import sys
import os
import httpx
import jwt
sys.stdout.reconfigure(encoding='utf-8')
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")

async def main():
    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage
    from src.core.auth.jwt_config import get_jwt_secret, get_jwt_algorithms

    settings = Settings()
    jwt_secret = get_jwt_secret(settings.authentication.jwt.secret)
    jwt_algorithms = get_jwt_algorithms(settings.authentication.jwt.algorithms)

    print("=" * 60)
    print("AUTHENTICATION & API DIAGNOSIS")
    print("=" * 60)

    # 1. Check JWT configuration
    print("\n=== 1. JWT Configuration ===")
    print(f"  JWT Secret (first 10 chars): {jwt_secret[:10]}...")
    print(f"  JWT Algorithms: {jwt_algorithms}")

    # 2. Test login and get token
    print("\n=== 2. Login Test ===")
    base_url = "http://localhost:8080"

    async with httpx.AsyncClient() as client:
        # Try login with correct password
        print("  Attempting login with misaya.yang...")
        login_resp = await client.post(
            f"{base_url}/api/v1/auth/login",
            json={"email": "misaya.yang@hejazfs.com.au", "password": "123456.dc"}
        )

        if login_resp.status_code != 200:
            print(f"  Login failed: {login_resp.status_code}")
            print(f"  Response: {login_resp.text}")

            # Try with admin
            print("\n  Trying admin login...")
            login_resp = await client.post(
                f"{base_url}/api/v1/auth/login",
                json={"email": "admin@hejazfs.com.au", "password": "123456.dc"}
            )

            if login_resp.status_code != 200:
                print(f"  Admin login also failed: {login_resp.status_code}")
                print(f"  Response: {login_resp.text}")

                # Check database directly for user info
                print("\n=== Checking Database Directly ===")
                db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
                await db.connect()

                users = await db._pool.fetch("SELECT user_id, email, password_hash, status FROM users")
                print(f"  Users in DB: {len(users)}")
                for u in users:
                    print(f"    {u['user_id']}: {u['email']}, status={u['status']}")
                    print(f"      password_hash: {u['password_hash'][:30]}...")

                await db.close()
                return

        token = login_resp.json()["access_token"]
        user_info = login_resp.json()["user"]
        print(f"  Login SUCCESS!")
        print(f"  User: {user_info}")
        print(f"  Token (first 50 chars): {token[:50]}...")

        # 3. Decode and verify token
        print("\n=== 3. Token Decode ===")
        try:
            payload = jwt.decode(token, jwt_secret, algorithms=jwt_algorithms)
            print(f"  Decoded payload:")
            for k, v in payload.items():
                if k not in ['exp', 'iat', 'jti']:
                    print(f"    {k}: {v}")

            token_tenant_id = payload.get("tenant_id", "NOT SET")
            print(f"\n  *** TOKEN tenant_id: '{token_tenant_id}' ***")
        except Exception as e:
            print(f"  Token decode FAILED: {e}")
            return

        # 4. Test API with token
        print("\n=== 4. API Test with Token ===")
        headers = {"Authorization": f"Bearer {token}"}

        # Test /auth/me
        resp = await client.get(f"{base_url}/api/v1/auth/me", headers=headers)
        print(f"  /auth/me: {resp.status_code}")
        if resp.status_code == 200:
            me_data = resp.json()
            print(f"    user_id: {me_data.get('user_id')}")
            print(f"    roles: {me_data.get('roles')}")

        # Test connections
        resp = await client.get(f"{base_url}/api/v1/confluence/connections", headers=headers)
        print(f"\n  /confluence/connections: {resp.status_code}")
        connections = resp.json()
        print(f"    Returned {len(connections)} connections")
        for c in connections:
            print(f"      - {c.get('name')}: tenant_id={c.get('tenant_id')}")

        # Test bindings
        resp = await client.get(f"{base_url}/api/v1/confluence/bindings", headers=headers)
        print(f"\n  /confluence/bindings: {resp.status_code}")
        bindings = resp.json()
        print(f"    Returned {len(bindings)} bindings")

    # 5. Direct database check
    print("\n=== 5. Direct Database Query ===")
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    # Query connections with explicit tenant_id filter
    print(f"\n  Querying with tenant_id='{token_tenant_id}':")
    conns = await db.list_confluence_connections(tenant_id=token_tenant_id)
    print(f"    Found {len(conns)} connections")

    print(f"\n  Querying with tenant_id=None (no filter):")
    conns_all = await db.list_confluence_connections(tenant_id=None)
    print(f"    Found {len(conns_all)} connections")
    for c in conns_all:
        print(f"      - {c.get('name')}: tenant_id='{c.get('tenant_id')}'")

    # Check if tenant_id values match
    print("\n=== 6. Tenant ID Comparison ===")
    print(f"  Token tenant_id: '{token_tenant_id}' (type: {type(token_tenant_id).__name__})")
    print(f"  DB tenant_ids: {[c.get('tenant_id') for c in conns_all]}")

    if token_tenant_id and conns_all:
        db_tenant = conns_all[0].get('tenant_id')
        if token_tenant_id == db_tenant:
            print(f"  MATCH: Token tenant matches DB tenant")
        else:
            print(f"  MISMATCH!")
            print(f"    Token: '{token_tenant_id}' (repr: {repr(token_tenant_id)})")
            print(f"    DB:    '{db_tenant}' (repr: {repr(db_tenant)})")
            print(f"    Equal: {token_tenant_id == db_tenant}")

    await db.close()
    print("\n" + "=" * 60)
    print("DIAGNOSIS COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
