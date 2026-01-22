# -*- coding: utf-8 -*-
"""Test API with and without auth"""
import asyncio
import sys
import os
import httpx
sys.stdout.reconfigure(encoding='utf-8')
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")

async def main():
    base_url = "http://localhost:8080"

    async with httpx.AsyncClient() as client:
        # Test 1: Without auth (anonymous)
        print("=== 1. Get Connections (no auth) ===")
        resp = await client.get(f"{base_url}/api/v1/confluence/connections")
        print(f"  Status: {resp.status_code}")
        print(f"  Response: {resp.json()}")

        # Test 2: Get auth/me to see user context
        print("\n=== 2. Get /auth/me (no auth) ===")
        resp = await client.get(f"{base_url}/api/v1/auth/me")
        print(f"  Status: {resp.status_code}")
        if resp.status_code == 200:
            print(f"  Response: {resp.json()}")
        else:
            print(f"  Response: {resp.text[:200]}")

if __name__ == "__main__":
    asyncio.run(main())
