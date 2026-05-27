"""
One-shot migration: local SQLite (data/bot.db) -> Supabase Postgres.

SQLite is the authoritative complete dataset. Supabase app tables are a
partial/broken copy. This script TRUNCATEs the Supabase app tables and
reloads every row from SQLite.

SKIPPED on purpose:
  - users      : keep the working Supabase auth user
  - bot_state  : keep Supabase halted=True (do not un-halt the bot)

Run:
  ./venv/Scripts/python.exe migrate_sqlite_to_supabase.py            # dry-run (counts only)
  ./venv/Scripts/python.exe migrate_sqlite_to_supabase.py --apply    # perform migration
"""
import asyncio
import sqlite3
import sys
from datetime import datetime
from uuid import uuid4

from app.config import settings
from app.utils.db_dialect import to_async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SQLITE_PATH = "data/bot.db"

# Load order: parents before children (FK-safe).
LOAD_ORDER = [
    "instagram_accounts",
    "campaigns",
    "campaign_accounts",
    "followers",
    "messages",
    "global_contacts",
    "activity_logs",
    "anomalies",
]
# Truncate order: children before parents.
TRUNCATE_ORDER = list(reversed(LOAD_ORDER))


def parse_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # SQLite sometimes stores 'YYYY-MM-DD HH:MM:SS' without micros
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise


async def get_pg_meta(conn, table):
    """Return (columns_in_order, boolean_cols set, timestamp_cols set)."""
    r = await conn.exec_driver_sql(
        "select column_name,data_type from information_schema.columns "
        f"where table_schema='public' and table_name='{table}' order by ordinal_position"
    )
    cols, bools, ts = [], set(), set()
    for name, dtype in r:
        cols.append(name)
        if dtype == "boolean":
            bools.add(name)
        elif dtype.startswith("timestamp"):
            ts.add(name)
    return cols, bools, ts


def read_sqlite(table):
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(f"select * from {table}")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


async def main(apply: bool):
    engine = create_async_engine(
        to_async_database_url(settings.database_url),
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__a_{uuid4()}__",
            "timeout": 30,
        },
    )

    plan = []
    async with engine.connect() as conn:
        for t in LOAD_ORDER:
            pg_cols, _, _ = await get_pg_meta(conn, t)
            src = read_sqlite(t)
            r = await conn.exec_driver_sql(f"select count(*) from {t}")
            plan.append((t, len(src), r.scalar(), pg_cols))

    print("=== PLAN (table: sqlite_rows -> supabase_now) ===")
    for t, n_src, n_dst, _ in plan:
        print(f"  {t:20s} {n_src:6d} -> {n_dst:6d}")

    if not apply:
        print("\nDRY-RUN. Re-run with --apply to perform migration.")
        await engine.dispose()
        return

    # --- Read + transform all tables, then FK-sanitize orphan rows ---
    raw = {t: read_sqlite(t) for t in LOAD_ORDER}

    account_ids = {r["id"] for r in raw["instagram_accounts"]}
    campaign_ids = {r["id"] for r in raw["campaigns"]}

    def drop(table, keep_fn, reason):
        before = len(raw[table])
        raw[table] = [r for r in raw[table] if keep_fn(r)]
        removed = before - len(raw[table])
        if removed:
            print(f"  [sanitize] {table}: dropped {removed} orphan rows ({reason})")

    # FK: campaign_accounts -> campaigns, instagram_accounts (NOT NULL) => drop orphans
    drop("campaign_accounts",
         lambda r: r["campaign_id"] in campaign_ids and r["account_id"] in account_ids,
         "missing campaign/account")
    # FK: followers -> campaigns (NOT NULL) => drop orphans
    drop("followers", lambda r: r["campaign_id"] in campaign_ids, "missing campaign")
    follower_ids = {r["id"] for r in raw["followers"]}
    # FK: messages -> campaigns, followers, instagram_accounts => drop orphans
    drop("messages",
         lambda r: r["campaign_id"] in campaign_ids
         and r["follower_id"] in follower_ids
         and r["account_id"] in account_ids,
         "missing campaign/follower/account")

    # activity_logs / anomalies: account_id & campaign_id are nullable FKs
    # => null out dangling references instead of dropping the row
    for table in ("activity_logs", "anomalies"):
        nulled = 0
        for r in raw[table]:
            if r.get("account_id") and r["account_id"] not in account_ids:
                r["account_id"] = None
                nulled += 1
            if r.get("campaign_id") and r["campaign_id"] not in campaign_ids:
                r["campaign_id"] = None
                nulled += 1
        if nulled:
            print(f"  [sanitize] {table}: nulled {nulled} dangling FK refs")

    async with engine.begin() as conn:
        # Single transaction: truncate all, then reload all. Rollback on any error.
        tlist = ", ".join(f"public.{t}" for t in TRUNCATE_ORDER)
        print(f"\nTRUNCATE {tlist} ...")
        await conn.exec_driver_sql(f"TRUNCATE {tlist}")

        for t in LOAD_ORDER:
            pg_cols, bool_cols, ts_cols = await get_pg_meta(conn, t)
            rows = raw[t]
            if not rows:
                print(f"  {t}: 0 rows, skip")
                continue
            cols = [c for c in pg_cols if c in rows[0]]
            placeholders = ", ".join(f":{c}" for c in cols)
            collist = ", ".join(cols)
            sql = f"INSERT INTO public.{t} ({collist}) VALUES ({placeholders})"

            batch = []
            for r in rows:
                rec = {}
                for c in cols:
                    v = r.get(c)
                    if c in bool_cols and v is not None:
                        v = bool(v)
                    elif c in ts_cols:
                        v = parse_dt(v)
                    rec[c] = v
                batch.append(rec)

            stmt = text(sql)
            CHUNK = 200
            for i in range(0, len(batch), CHUNK):
                await conn.execute(stmt, batch[i:i + CHUNK])
            print(f"  {t}: inserted {len(batch)} rows")

    # Verify
    async with engine.connect() as conn:
        print("\n=== VERIFY (sqlite vs supabase) ===")
        ok = True
        for t in LOAD_ORDER:
            n_exp = len(raw[t])
            r = await conn.exec_driver_sql(f"select count(*) from {t}")
            n_dst = r.scalar()
            flag = "OK" if n_exp == n_dst else "MISMATCH"
            if n_exp != n_dst:
                ok = False
            print(f"  {t:20s} expected {n_exp:6d} == supabase {n_dst:6d}  {flag}")
        print("\nRESULT:", "ALL OK" if ok else "MISMATCH - investigate")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
