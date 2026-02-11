import asyncio
import json

import httpx

BASE_URL = "http://localhost:8080"
ADMIN_API_KEY = "gw_gEtIPdAxdXI4D-WyWxvgFNPkdd7CU2VPdeFg9XdqFhs"


async def main():
    # Disable SSL verification to avoid permission errors in sandbox
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0, verify=False) as client:
        # 0. Login as Admin
        print("--- Logging in as Admin ---")
        admin_token = None
        try:
            # Try login
            resp = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": "admin@hejazfs.com.au",  # Try email
                    "password": "123456.dc",
                },
            )
            if resp.status_code == 404:
                # Try without /api prefix
                resp = await client.post(
                    "/v1/auth/login",
                    json={"email": "admin@hejazfs.com.au", "password": "123456.dc"},
                )

            if resp.status_code == 401:
                # Try username 'admin'
                resp = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "email": "admin",  # Schema says email, but maybe backend handles it
                        "password": "123456.dc",
                    },
                )

            # Wait, auth.py schema says `LoginRequest` takes `email` and `password`.
            # And `login` function handles `login_email = body.email` and fallback.
            # So I should send `email: "admin"`.

            # Actually, let's try just "admin" as email field, the code says:
            # login_prefix = login_email.split("@")[0]
            # user = await db.get_user(login_prefix)

            resp = await client.post(
                "/api/v1/auth/login", json={"email": "admin", "password": "123456.dc"}
            )

            resp.raise_for_status()
            admin_token = resp.json()["access_token"]
            print("Admin logged in successfully.")
        except Exception as e:
            print(f"Admin login failed: {e}")
            if hasattr(e, "response") and e.response:
                print(e.response.text)
            return

        headers = {"Authorization": f"Bearer {admin_token}"}

        # 1. List Users
        print("--- Listing Users ---")
        try:
            resp = await client.get("/api/v1/users", headers=headers)
            if resp.status_code == 404:
                # Try without /api prefix if router prefix is different
                resp = await client.get("/v1/users", headers=headers)

            resp.raise_for_status()
            users_data = resp.json()
            users = users_data.get("users", [])
            print(f"Found {len(users)} users.")

            user2 = None
            for u in users:
                if u["user_id"] != "admin":
                    user2 = u
                    break

            if not user2:
                print("Creating second user...")
                create_resp = await client.post(
                    "/v1/users",
                    headers=headers,
                    json={
                        "email": "test_user_2@example.com",
                        "display_name": "Test User 2",
                        "roles": ["user"],
                    },
                )
                create_resp.raise_for_status()
                user2 = create_resp.json()
                print(f"Created user: {user2['user_id']}")

            # 2. Authenticate as User 2 (Login)
            # Default password is usually 111111 for new users
            print(f"Logging in as {user2['user_id']}...")
            login_resp = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": user2["email"] if "email" in user2 else user2["user_id"],
                    "password": "111111",  # Default password from users.py
                },
            )
            if login_resp.status_code == 400 and "change password" in login_resp.text.lower():
                # Handle forced password change if needed, or just ignore if we can't
                print(
                    "User 2 needs password change. Skipping specific User 2 login, using Admin for traffic simulation with User 2 context if possible."
                )
                # Actually, if we use X-API-Key, it's admin. To simulate user 2, we need their token.
                # Let's try to change their password first if we can (as admin).
                # Admin reset password endpoint: /v1/users/{id}/reset-password
                pass
            elif login_resp.status_code == 200:
                user2_token = login_resp.json()["access_token"]
                print("User 2 logged in successfully.")
            else:
                print(f"User 2 login failed: {login_resp.status_code} {login_resp.text}")
                user2_token = None

        except Exception as e:
            print(f"Error setting up users: {e}")
            return

        # 3. Generate Traffic
        print("\n--- Generating Traffic ---")

        # Traffic 1: Admin using Qwen Max (Assistant)
        # Endpoint: /api/v1/assistant/chat
        print("1. Admin calling Qwen Max (Assistant API)...")
        try:
            # Check schema from src/api/schemas/assistant.py
            # message: str, model_id: str
            chat_payload = {
                "model_id": "qwen-max",
                "message": "Hello, how much do you cost?",
                "temperature": 0.7,
                # "history": []
            }
            resp = await client.post("/api/v1/assistant/chat", headers=headers, json=chat_payload)
            if resp.status_code == 200:
                print("Admin Qwen Max request: Success")
                # print(resp.json())
            else:
                print(f"Admin Qwen Max request failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"Request error: {e}")

        # Traffic 2: User 2 using Gemini Flash (Flash Agent)
        # If Flash Agent is also an assistant model, we can use the same endpoint.
        # If it's a separate service, we might need /proxy or /invoke.
        # Assuming it's a model "gemini-2.5-flash".
        if user2_token:
            print("2. User 2 calling Gemini Flash...")
            user2_headers = {"Authorization": f"Bearer {user2_token}"}
            try:
                chat_payload = {
                    "model_id": "gemini-2.5-flash",
                    "message": "Quick answer please.",
                    "temperature": 0.1,
                }
                resp = await client.post(
                    "/api/v1/assistant/chat", headers=user2_headers, json=chat_payload
                )
                if resp.status_code == 200:
                    print("User 2 Gemini Flash request: Success")
                else:
                    print(f"User 2 Gemini Flash request failed: {resp.status_code} {resp.text}")
            except Exception as e:
                print(f"User 2 request error: {e}")

        # 4. Check Dashboard
        print("\n--- Checking Dashboard Stats ---")
        await asyncio.sleep(2)  # Wait for metrics to aggregate

        try:
            # Summary
            summary_resp = await client.get(
                "/api/v1/dashboard/summary?period=today", headers=headers
            )
            if summary_resp.status_code == 200:
                summary = summary_resp.json()
                print("Dashboard Summary:")
                print(json.dumps(summary["overview"], indent=2))

                # Check if we see traffic
                total = summary["overview"].get("total_requests", 0)
                if total > 0:
                    print("SUCCESS: Traffic detected in dashboard.")
                else:
                    print("WARNING: No traffic detected in dashboard summary.")
            else:
                print(f"Failed to get dashboard summary: {summary_resp.status_code}")

            # User stats
            if user2 and user2.get("user_id"):
                user_stats = await client.get(
                    f"/api/v1/dashboard/user/{user2['user_id']}", headers=headers
                )
                if user_stats.status_code == 200:
                    print(f"\nUser 2 Stats: {json.dumps(user_stats.json(), indent=2)}")
                else:
                    print(f"Failed to get User 2 stats: {user_stats.status_code}")

        except Exception as e:
            print(f"Error checking dashboard: {e}")


if __name__ == "__main__":
    asyncio.run(main())
