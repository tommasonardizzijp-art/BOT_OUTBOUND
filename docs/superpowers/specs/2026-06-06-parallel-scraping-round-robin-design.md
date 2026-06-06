# Spec — Scraping multi-account round-robin per-lead (Approccio C)

**Data**: 2026-06-06
**Branch**: feature/advanced-scraping
**Stato**: approvato, pronto per implementation plan

---

## Problema

Oggi, con 2+ account scraping assegnati alla stessa campagna, il carico **non** è condiviso: un solo account (A) esegue tutti i `user_info` per-lead; il secondo (B) interviene **solo** come fallback quando A raggiunge il cap giornaliero (`SCRAPE_DAILY_LIMIT`, default 180) o prende un 429/soft-block.

Conseguenze:
- Campagna < 180 lead → B non lavora mai.
- Campagna > 180 → A fa i primi 180, poi B i successivi, ecc. (blocchi sequenziali, non alternati).

Con account su **IP diversi** (proxy mobile per uno, residenziale per l'altro) ha senso far lavorare entrambi dall'inizio, dimezzando il footprint di ciascuno.

## Obiettivo

Far sì che tutti gli account scraping della campagna lavorino **a turno (round-robin) per-lead**, condividendo il carico fin dall'inizio. Struttura **seriale, un solo job** (no worker paralleli). Tempi condivisi, accettati dall'utente.

## Non-obiettivi (YAGNI)

- NO worker scraping paralleli indipendenti (era Approccio A — scartato).
- NO fase lista/bio disaccoppiata, NO stato `pending` intermedio, NO lock ottimistico per-follower.
- NO break per-account: il break resta **campagna-level** (preserva il box "Pausa sessione" + countdown UI attuale).
- NO nuove colonne DB, NO migrazioni.

---

## Comportamento atteso

1. A inizio scraping, **pre-login di tutti** gli account `scraping`/`both` della campagna → pool `{account_id: client}` in memoria, con slot scraping acquisito per ciascuno.
2. In `_store_followers_batch`, per ogni follower del batch si seleziona il **prossimo account dal pool** in ordine round-robin (A→B→A→B…) e si usa il suo client cached per `user_info_v1`.
3. Il delay `bio_fetch_delay_min/max` (per-campagna) resta applicato **dopo ogni lead**, globale: di fatto separa lead-su-A da lead-su-B. Ogni singolo account attende ~`(n_account × delay)` tra i propri lead.
4. **Cap per-account**: prima di usare un account si verifica `has_scrape_budget`. Se capped → rimosso dal pool attivo per il resto della sessione. Se il pool si svuota (tutti capped) → `ScrapeBudgetError` (comportamento attuale invariato → pausa `scrape_capped`).
5. **429 / soft-block** su un lead: si mantiene la logica retry esistente, ma la rotazione punta al **prossimo account del pool** (già loggato, niente re-login). Il conteggio `consecutive_soft_blocks` e la soglia di 3 restano invariati.
6. **Paginazione lista** (`_fetch_followers_chunk`): resta su **un account** del pool (es. il primo). Chiamate cheap, non serve ruotarle.
7. **Session break** campagna-level invariato: dopo `scrape_session_size` lead → `campaign.status = scraping_break` + `scrape_break_until`. Il box UI e il countdown restano come ora.
8. **Fine scraping**: rilascio di **tutti** gli slot del pool; salvataggio `session_data` per ogni account del pool.

---

## Punti tecnici / vincoli

- **No login thrashing**: il round-robin per-lead NON deve rifare login a ogni switch. I client sono pre-loggati una volta e tenuti nel pool. (Oggi `_switch_scraping_account` fa login a ogni rotazione — accettabile perché rara; per-lead sarebbe inaccettabile.)
- **Slot**: oggi 1 slot per volta. Il pool tiene 1 slot per ciascun account contemporaneamente. Va gestita l'acquisizione multipla a inizio sessione e il rilascio multiplo a fine. Se uno slot è già occupato da un'altra campagna, l'account viene escluso dal pool (con warning), non blocca gli altri.
- **Memoria**: 2 client instagrapi pre-loggati in RAM. Trascurabile.
- **Salvataggio sessione**: oggi si salva `session_data` dell'account attivo a ogni commit batch. Col pool, salvare la sessione di ciascun account del pool (almeno a fine batch / fine scraping).
- **Compatibilità**: con 1 solo account scraping il pool ha 1 elemento → comportamento identico a oggi. Nessuna regressione per campagne mono-account.
- **Import mode**: fuori scope. `import_resolver.py` resta invariato (resolve seriale singolo account). Eventuale estensione futura.

---

## Modifiche UI (helper testo delay)

Aggiungere un avviso esplicito accanto ai campi `bio_fetch_delay_min/max`, in **entrambi**:
- form nuova campagna (componente CampaignForm)
- modale impostazioni (ingranaggio) in `frontend/app/campaigns/[id]/page.tsx`

Testo:

> ⚠️ **Questi tempi valgono per OGNI lead estratto, condivisi tra tutti gli account scraping.** Con più account il delay si applica tra un account e il successivo: ogni singolo account aspetta circa (n° account × questo valore) tra i suoi lead. Esempio: 2 account e vuoi ~6-10s per account → imposta **3-5s**.

---

## File coinvolti

- `backend/app/services/scraper.py` — pool pre-login, round-robin per-lead in `_store_followers_batch`, gestione slot multipli, rotazione 429/cap verso il pool, salvataggio sessioni multiple. Cuore della modifica.
- `frontend/app/campaigns/[id]/page.tsx` — helper text nel modale impostazioni.
- Componente form nuova campagna (CampaignForm) — helper text.
- `backend/tests/test_robustness_instagrapi.py` / nuovo test — copertura round-robin.

## Test

- **Unit**: con pool di 2 account mockati, verificare che i `user_info` si alternino A→B→A→B; che un account capped esca dal giro; che pool vuoto → `ScrapeBudgetError`.
- **Unit**: con pool di 1 account, comportamento identico a oggi (no regressione).
- **Unit**: 429 su lead di A → prossimo lead usa B senza nuovo login (verificare che `_login` non venga richiamato).
- **E2e**: campagna 2 account, scrape di N lead → conteggio `scrape_lookups_today` distribuito ~equamente tra i due account.

---

## Rischi & mitigazioni

| Rischio | Mitigazione |
|---|---|
| Slot multipli lasciati appesi su crash | Rilascio in `finally`; cron stale-lock esistente non copre slot → verificare cleanup allo startup guard |
| Un account del pool perde la sessione a metà (LoginRequired) | Re-login lazy di quel solo account; se fallisce, escludilo dal pool e continua con gli altri |
| Utente non riduce i delay → account troppo lenti | Helper text esplicito (sopra) |
| Regressione mono-account | Test dedicato pool=1 |
