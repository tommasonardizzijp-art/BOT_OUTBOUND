# ARCHITETTURA BROWSER (Patchright)

Layer `app/browser/` e flusso di invio di un DM. Estratto da `CLAUDE.md` il 2026-07-29 (contenuto invariato).

---

Il layer browser in `app/browser/` gestisce:

- **`context_manager.py`**: pool profili Chromium, 1 profilo per account, canvas noise injection
- **`fingerprint.py`**: fingerprint deterministico per account (viewport, UA, timezone, locale)
- **`instagram_page.py`**: Page Object Model per Instagram web

## Flusso `send_dm`
1. `page.goto(profile_url)` → carica profilo target
2. `_simulate_browsing()` → scroll randomizzato (4 tipi: scroll piccolo, scroll grande, pausa lettura, hover) per `pre_dm_browse_seconds()` secondi (lognormale ~12s)
3. `window.scrollTo(0,0)` → risale in cima (il pulsante Message è nell'header del profilo)
4. Click su `div[role="button"]:text-is("Message")` (match esatto, non `has-text`)
5. `wait_for_url('/direct/')` → attende navigazione alla thread DM
6. Dismiss popup vari ("Not Now", "Cancel", ecc.)
7. `_human_type()` → typing lognormale con pause tra parole; gli a-capo del messaggio vengono battuti come `Shift+Enter` (newline senza invio), tipando riga per riga
8. `Enter` → invio

> Patchright richiede il download di Chromium: `patchright install chromium`.

---

Vedi anche: [PRINCIPI_ANTI_DETECTION.md](PRINCIPI_ANTI_DETECTION.md) · [AI_ARCHITECTURE.md](AI_ARCHITECTURE.md) · [SCALA_E_PARALLELISMO.md](SCALA_E_PARALLELISMO.md)
