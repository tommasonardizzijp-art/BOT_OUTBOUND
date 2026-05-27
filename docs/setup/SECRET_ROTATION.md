# Rotazione segreti — OBBLIGATORIA (segreti esposti)

I segreti in `.env` e in `backups/BOT_OUTBOUND_BACKUP/.env` sono stati esposti
(duplicati nel working dir + visibili in sessioni di tool). Vanno **ruotati tutti**.

1. **Supabase DB password** (`DATABASE_URL`)
   - Dashboard Supabase → Database → Reset database password.
   - Aggiornare `DATABASE_URL` in `.env`.

2. **SECRET_KEY (Fernet)** — cifra le password account Instagram
   - Generare: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - ⚠️ Cambiare questa chiave rende illeggibili le `encrypted_password` esistenti:
     ri-inserire le password degli account IG dopo il cambio, **oppure** uno
     script di re-encrypt che decifra con la vecchia chiave e ricifra con la nuova.

3. **JWT_SECRET**
   - Generare: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - Invalida i token attivi: tutti gli utenti dovranno rifare login.

4. **TELEGRAM_BOT_TOKEN**
   - @BotFather → `/revoke` → genera nuovo token → aggiornare `.env`.

5. **AI_API_KEY (Groq)**
   - console.groq.com → revoca la key esposta → crea nuova key → aggiornare `.env`.

## Dopo la rotazione

- Eliminare la cartella `backups/BOT_OUTBOUND_BACKUP/` dal working dir
  (azione **distruttiva e irreversibile** — eseguire solo previa conferma esplicita
  e dopo aver verificato che non contenga dati unici non presenti altrove).
- Verificare che `.gitignore` ignori `.env`, `.env.*`, `backups/BOT_OUTBOUND_BACKUP/`,
  `backend/data/`, `.vercel`.
- Se in futuro si fa `git init`: NON committare prima di aver confermato che
  `git status` non elenchi i file sopra.
