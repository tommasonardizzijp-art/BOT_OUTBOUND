"""CLI to create the first admin user.

Usage from `backend/`:
    python -m scripts.create_admin --email admin@example.com --password 'somesecret'

If --password is omitted, prompts interactively (input is not echoed).
"""
import argparse
import asyncio
import getpass
import sys

# Allow `python scripts/create_admin.py` without the -m flag too.
sys.path.insert(0, ".")

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.security import hash_password  # noqa: E402


async def _run(email: str, password: str, role: str) -> int:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(User).where(User.email == email))
        if existing:
            print(f"User {email} already exists — updating password and role={role}")
            existing.password_hash = hash_password(password)
            existing.role = role
            existing.is_active = True
        else:
            db.add(User(
                email=email,
                password_hash=hash_password(password),
                role=role,
            ))
        await db.commit()
    print(f"OK — {role} user '{email}' ready.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default=None, help="If omitted, prompt securely.")
    parser.add_argument("--role", default="admin", choices=["admin", "operator"])
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        return 1

    return asyncio.run(_run(args.email, password, args.role))


if __name__ == "__main__":
    sys.exit(main())
