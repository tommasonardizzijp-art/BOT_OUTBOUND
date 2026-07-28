# CLAUDE.md — BOT OUTBOUND

Agente di outreach su Instagram: scraping follower/following di una pagina target (o lista importata), un messaggio per profilo (template locale o AI), invio DM con comportamento umano. FastAPI + ARQ, DB Supabase, frontend Next.js. Gira in locale, non è deployato.

## Dove leggere

| Ti serve | Leggi |
|---|---|
| Stato globale, ordine di lettura dei doc | [INDEX.md](INDEX.md) |
| Cronologia dei lavori + stato fasi | [PROGRESS.md](docs/project/PROGRESS.md) |
| Stack, mappa dei file, convenzioni codice | [OVERVIEW.md](docs/architecture/OVERVIEW.md) |
| Tabelle, colonne, enum di stato, migrazioni | [DATABASE.md](docs/architecture/DATABASE.md) |
| Come nasce il testo di un DM (template vs AI) | [AI_ARCHITECTURE.md](docs/architecture/AI_ARCHITECTURE.md) · [AI_MESSAGES.md](docs/architecture/AI_MESSAGES.md) |
| Multi-account, two-phase scraping, import, kill-switch | [SCALA_E_PARALLELISMO.md](docs/architecture/SCALA_E_PARALLELISMO.md) |
| Timing anti-ban, vettori di detection, proxy | [PRINCIPI_ANTI_DETECTION.md](docs/architecture/PRINCIPI_ANTI_DETECTION.md) · [ANTI_DETECTION.md](docs/architecture/ANTI_DETECTION.md) · [PROXY_MOBILE_SETUP.md](docs/setup/PROXY_MOBILE_SETUP.md) |
| Layer browser Patchright, flusso `send_dm` | [BROWSER.md](docs/architecture/BROWSER.md) |
| `.env`, migrazioni, avvio dei 5 processi | [CONFIGURAZIONE.md](docs/setup/CONFIGURAZIONE.md) |
| Canale WhatsApp (design e stato) | [SDD-whatsapp-channel.md](docs/whatsapp/SDD-whatsapp-channel.md) |

## Regole sempre valide

- **Prima di toccare un flusso**: rileggi il codice coinvolto e il contesto recente da `INDEX.md`. I doc possono essere indietro rispetto al codice: se divergono **non rimuovere un guardrail per aderire al documento** — capisci perché quel codice esiste, e se non distingui una scelta intenzionale da un residuo chiedi all'utente.
- **A fine operazione** (fix, feature, refactor, debug), nello stesso task e **non opzionale**: sezione datata in `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` (cosa modificato, root cause, file toccati, comportamento atteso) + indice `MEMORY.md`; poi riallinea i doc resi obsoleti (`INDEX.md`, `PROGRESS.md`, il file `docs/` coinvolto, questo file se cambiano struttura o regole).
- **Migrazioni prima del codice**: i model dichiarano colonne che il DB può non avere → `python -m scripts.migrate` prima di far girare il codice nuovo; ferma bot e backend zombie prima (un `idle in transaction` blocca gli `ALTER TABLE`).
- **Timing e simulazione umana non si toccano a intuito**: leggi prima i principi anti-detection. Mai oltre 20-30 DM/giorno per account.
- **Segreti**: `.env` mai committato; mai loggare password, `session_data` o `SECRET_KEY`.
- **Codice**: async ovunque, `Depends(get_db)`, `loguru` mai `print()`, niente lazy loading ORM.
- **Git**: branch dedicato + PR, mai push diretto su `main`.

Architettura o schema cambiati? → il file `docs/` corrispondente. Stato e cronologia? → `PROGRESS.md`. **Questo file resta corto: panoramica, link, regole always-on.**
