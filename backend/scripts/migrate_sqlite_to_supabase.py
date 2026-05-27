"""Copy local SQLite data into a migrated Supabase/Postgres database.

Prerequisites:
1. Set SUPABASE_DATABASE_URL or pass --postgres-url.
2. Run Alembic migrations against Supabase first.
3. Stop backend/worker while copying to avoid changing SQLite mid-export.

Example:
  python -m scripts.migrate_sqlite_to_supabase --sqlite ./data/bot.db --truncate
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sqlite3
from typing import Any


TABLES = [
    "instagram_accounts",
    "campaigns",
    "campaign_accounts",
    "followers",
    "messages",
    "activity_logs",
    "global_contacts",
    "anomalies",
    "users",
    "bot_state",
]

BOOL_COLUMNS = {
    ("campaign_accounts", "is_active"),
    ("campaigns", "require_approval"),
    ("campaigns", "auto_generate"),
    ("followers", "is_private"),
    ("followers", "is_verified"),
    ("users", "is_active"),
    ("bot_state", "halted"),
}

DATETIME_SUFFIXES = ("_at", "_until")
DATE_COLUMNS = {("instagram_accounts", "warmup_advanced_date")}


def _sqlite_path(value: str) -> Path:
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if value.startswith(prefix):
            return Path(value.removeprefix(prefix))
    return Path(value)


def _asyncpg_url(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _convert(table: str, column: str, value: Any) -> Any:
    if value is None:
        return None
    if (table, column) in BOOL_COLUMNS:
        return bool(value)
    if column.endswith(DATETIME_SUFFIXES) and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    if (table, column) in DATE_COLUMNS:
        return value
    return value


def _read_rows(sqlite_file: Path, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    conn = sqlite3.connect(sqlite_file)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return [], []
        cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        rows = []
        for row in conn.execute(f'SELECT * FROM "{table}"').fetchall():
            rows.append(tuple(_convert(table, col, row[col]) for col in cols))
        return cols, rows
    finally:
        conn.close()


async def _copy_table(pg, sqlite_file: Path, table: str) -> int:
    cols, rows = _read_rows(sqlite_file, table)
    if not cols:
        return 0
    if not rows:
        return 0
    quoted = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
    sql = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
    try:
        await pg.executemany(sql, rows)
        return len(rows)
    except Exception:
        # Batch failed (e.g. FK violation on orphaned rows) — retry row-by-row, skip bad ones
        inserted = 0
        skipped = 0
        for row in rows:
            try:
                await pg.execute(sql, *row)
                inserted += 1
            except Exception as e:
                skipped += 1
                if skipped <= 3:
                    print(f"  [{table}] skipped row (id={row[0]}): {str(e)[:120]}")
        if skipped:
            print(f"  [{table}] {skipped} rows skipped (orphaned/invalid FK)")
        return inserted


async def main() -> None:
    parser = argparse.ArgumentParser(description="Copy SQLite BOT OUTBOUND data to Supabase/Postgres.")
    parser.add_argument("--sqlite", default=os.getenv("SQLITE_DATABASE_URL", "./data/bot.db"))
    parser.add_argument("--postgres-url", default=os.getenv("SUPABASE_DATABASE_URL"))
    parser.add_argument("--truncate", action="store_true", help="TRUNCATE target tables before inserting.")
    args = parser.parse_args()

    if not args.postgres_url:
        raise SystemExit("Missing --postgres-url or SUPABASE_DATABASE_URL")
    sqlite_file = _sqlite_path(args.sqlite)
    if not sqlite_file.exists():
        raise SystemExit(f"SQLite DB not found: {sqlite_file}")

    try:
        import asyncpg
    except ImportError as exc:
        raise SystemExit("Install asyncpg first: pip install asyncpg") from exc

    pg = await asyncpg.connect(_asyncpg_url(args.postgres_url), statement_cache_size=0)
    try:
        if args.truncate:
            for table in reversed(TABLES):
                await pg.execute(f'TRUNCATE TABLE "{table}" CASCADE')
        for table in TABLES:
            count = await _copy_table(pg, sqlite_file, table)
            print(f"{table}: {count}")
    finally:
        await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
