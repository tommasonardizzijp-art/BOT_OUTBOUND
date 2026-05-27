# Migrazione DB: SQLite → Supabase Postgres

> **Prerequisito:** prima configura e testa tutto in locale con SQLite.
> Migra a Supabase solo quando vuoi accedere al frontend da altri dispositivi.

## 1. Crea progetto Supabase

1. Vai su [supabase.com](https://supabase.com) → Sign up → New project
2. Scegli nome (es. `bot-outbound`), password DB, regione (EU West per l'Italia)
3. Aspetta ~2 min che il progetto si inizializzi

## 2. Ottieni la connection string

In Supabase dashboard → **Settings → Database → Connection string → URI**

Seleziona **"Connection pooling" (Transaction mode)** per produzione:
```
postgresql://postgres.XXXX:[PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:6543/postgres
```

Per Alembic (migrations) usa **Session mode** (porta 5432):
```
postgresql://postgres.XXXX:[PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:5432/postgres
```

## 3. Installa driver asyncpg

```bash
cd backend
./venv/Scripts/pip.exe install asyncpg psycopg[binary]
```

Aggiungi a `requirements.txt`:
```
asyncpg>=0.29.0
psycopg[binary]>=3.1.0
```

## 4. Configura .env

```env
# Sostituisci la riga DATABASE_URL esistente
DATABASE_URL=postgresql+asyncpg://postgres.XXXX:[PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:6543/postgres
```

## 5. Esegui migrations su Supabase

```bash
cd backend
./venv/Scripts/python.exe -m alembic upgrade head
```

Verifica in Supabase → **Table Editor** che tutte le tabelle siano create.

## 6. Migra i dati da SQLite

Script di migrazione (da eseguire una sola volta):

```bash
cd backend
./venv/Scripts/python.exe scripts/migrate_sqlite_to_supabase.py \
  --sqlite data/bot.db \
  --postgres "postgresql+psycopg://postgres.XXXX:[PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
```

> Il script migra in ordine: accounts → campaigns → campaign_accounts → followers → messages → activity_logs → global_contacts → anomalies → users → bot_state
> Verifica conteggi pre/post a fine script.

## 7. RLS (Row Level Security)

La nostra strategia è **single-tenant** — la sicurezza sta nell'app layer (JWT Bearer).
Supabase di default ha RLS disabilitato sulle tabelle create via Alembic (via direct SQL).

**Configurazione minima consigliata:**

In Supabase → **SQL Editor**, esegui:

```sql
-- Blocca accesso anonimo a tutte le tabelle (la nostra app usa service_role)
ALTER TABLE instagram_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE followers ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE global_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE anomalies ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;

-- Il backend si connette come service_role (bypassa RLS) → ok.
-- Solo il ruolo anon/authenticated (frontend Supabase JS SDK) viene bloccato.
-- Dato che usiamo FastAPI direttamente, questo è corretto.
```

> **Nota:** Il backend usa la connection string direttamente con il ruolo `postgres`
> (service_role), che bypassa RLS. Questo è il comportamento corretto — la nostra
> app gestisce auth internamente con JWT. RLS blocca solo accessi diretti tramite
> Supabase JS SDK (che non usiamo).

## 8. Variabili da aggiornare nel frontend

Se usi Vercel per il frontend, aggiorna l'env var:
```env
NEXT_PUBLIC_API_URL=https://api-bot.tuodominio.com/api
```

## 9. Test dopo migrazione

```bash
# Verifica che il backend parta e i dati ci siano
cd backend
./venv/Scripts/python.exe -c "
import asyncio
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from sqlalchemy import select, func

async def test():
    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count(InstagramAccount.id)))
        print(f'Accounts in DB: {count}')

asyncio.run(test())
"
```

## Note importanti

- **WAL mode PRAGMA** (SQLite-only): `backend/app/database.py` applica i PRAGMA solo se dialect è SQLite — già gestito nel codice tramite `to_async_database_url()`.
- **`sqlite_insert` in orchestrator**: `_try_reserve_global_contact` usa `sqlalchemy.dialects.sqlite.insert` → andrà aggiornato per Postgres quando si migra. Usa `backend/app/utils/db_dialect.py` (da implementare) oppure il file già esiste nel repo.
- **Backup prima di migrare**: esegui `cp data/bot.db data/bot_backup_$(date +%Y%m%d).db` prima di qualsiasi operazione.
