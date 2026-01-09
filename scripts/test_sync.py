# -*- coding: utf-8 -*-
"""Test Confluence sync with image processing"""
import asyncio
import sys
sys.stdout.reconfigure(encoding='utf-8')

import httpx

BASE_URL = "http://localhost:8080"

async def main():
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get bindings
        print("=== Getting Confluence Bindings ===")
        resp = await client.get(f"{BASE_URL}/api/v1/confluence/connections")
        print(f"Connections response: {resp.status_code}")
        if resp.status_code == 200:
            connections = resp.json()
            print(f"Connections: {connections}")

        # Try to get binding for HFDSH space
        print("\n=== Getting Binding Details ===")
        resp = await client.get(f"{BASE_URL}/api/v1/confluence/bindings")
        print(f"Bindings response: {resp.status_code}")
        if resp.status_code == 200:
            bindings = resp.json()
            print(f"Bindings: {bindings}")

            # If we have bindings, try to sync one
            if bindings:
                binding_id = bindings[0].get("binding_id")
                print(f"\n=== Triggering Sync for {binding_id} ===")
                resp = await client.post(f"{BASE_URL}/api/v1/confluence/bindings/{binding_id}/sync")
                print(f"Sync response: {resp.status_code}")
                print(f"Sync result: {resp.text[:500] if resp.text else 'empty'}")

if __name__ == "__main__":
    asyncio.run(main())
