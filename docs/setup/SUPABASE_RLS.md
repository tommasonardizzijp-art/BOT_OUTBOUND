# Supabase RLS Guide

This project should treat Supabase as a backend-only database. The Next.js
frontend must keep calling FastAPI; it must not connect directly to Supabase
with an anon key.

## Recommended Setup

1. Create the Supabase project.
2. Copy the pooled Postgres connection string.
3. Set `DATABASE_URL=postgresql+asyncpg://...` in `.env`.
4. Install backend requirements: `pip install -r backend/requirements.txt`.
5. Start the backend once so Alembic runs all migrations.
6. If migrating existing local data, run:

```bash
cd backend
python -m scripts.migrate_sqlite_to_supabase --sqlite ./data/bot.db --postgres-url "%SUPABASE_DATABASE_URL%" --truncate
```

## RLS Policy

Because FastAPI is the only database client, enable RLS and deny direct client
access by default. Use the Supabase service role only on the backend side if
you ever add direct Supabase APIs.

Suggested SQL baseline:

```sql
alter table instagram_accounts enable row level security;
alter table campaigns enable row level security;
alter table campaign_accounts enable row level security;
alter table followers enable row level security;
alter table messages enable row level security;
alter table activity_logs enable row level security;
alter table global_contacts enable row level security;
alter table anomalies enable row level security;
alter table users enable row level security;
alter table bot_state enable row level security;
```

Do not add permissive anon policies for these tables. If a future feature needs
direct browser access, create a dedicated read-only view with a narrow policy
instead of exposing operational tables.

## Operational Notes

- Keep `JWT_SECRET` enabled in production.
- Keep `SECRET_KEY` unchanged when moving databases, or encrypted IG passwords
  cannot be decrypted.
- Stop backend and ARQ worker before exporting SQLite data.
- Run migrations before the copy script, not after.
- If using the Supabase Pooler/PgBouncer connection string, asyncpg prepared
  statements must be disabled. The backend now applies pooler-safe settings
  automatically (`prepared_statement_cache_size=0`, `statement_cache_size=0`,
  unique prepared statement names, and `NullPool` for Postgres).
- After switching `DATABASE_URL` to Supabase, ensure the `users` table contains
  at least one active admin. If SQLite users were not migrated, recreate one:

```bash
cd backend
python -m scripts.create_admin --email admin@example.com --role admin
```
