import asyncio
import os
from src.core.auth.password import hash_password
from src.persistence.database import db

async def main():
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL must be set")
    await db.connect()
    pwd_hash = hash_password("123456.dc")
    await db.execute(
        "INSERT INTO users (user_id, email, password_hash, status, roles) VALUES ($1, $2, $3, $4, $5)",
        "admin", "admin@hejazfs.com.au", pwd_hash, "active", ["admin"]
    )
    print("Admin created")
    await db.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
