# WhatsApp M5 — QA finale + rampa volume + collaudo Primero — Implementation Plan

> **Per chi esegue:** REQUIRED SUB-SKILL: `sviluppo-modulo` (worktree isolato, implementer+reviewer dedicato per task, TDD, QA agent, protocollo di chiusura modulo — 20+ test manuali, 30+ adversarial, fix loop al 100%). REQUIRED SUB-SKILL: `superpowers:executing-plans` o `subagent-driven-development` per l'esecuzione task-by-task.
>
> **Scritto la notte del 05-06/08 mentre Tommaso non era reperibile** (autorizzato in anticipo). Nessuna riga di codice su numeri WhatsApp reali toccata scrivendo questo piano — e' solo pianificazione. Vedi [[sessioni/2026-08-06_botoutbound-incidente-wa-poc-profile-cancellato]] per l'incidente della stessa notte (irrilevante per il contenuto di questo piano, ma cambia lo stato del profilo `D:\dev\wa-poc\profile`: va ricreato da zero con nuovo QR).

## Goal

M5 e' l'ULTIMO cantiere prima del collaudo commerciale: chiude i tre buchi che restano aperti da M0 (rampa volume mai verificata, invio reale mai provato, nessun runbook operativo) e prepara la macchina legale/commerciale per il primo cliente pagante (Primero). Non e' un modulo di feature — e' un modulo di **verifica + operatività**.

## Cosa e' GIA' deciso (non ridiscutere)

Da SDD §14 (roadmap) e handoff 04/08:
- **Rampa volume**: 10 → 30 → 60 → 100 messaggi/giorno sulla prima campagna vera, **stop al primo warning** (non e' un traguardo, e' un test che puo' fallire a qualunque gradino).
- **`WA_SEND_ENABLED`** resta `false` finche' non c'e' un numero di SERVIZIO dedicato — mai il numero personale di Tommaso usato per M0-M4.
- **Script di purge per-tenant** (SDD §12.4, GDPR): "cliente X chiude" deve poter cancellare `wa_contacts`/`wa_messages`/`wa_inbound_events` + profilo browser del tenant.
- **Parere legale GDPR** (§12, Q89-Q96) e' una dipendenza ESTERNA, non blocca M0-M4 sul numero test, ma blocca il go-live commerciale multi-cliente.

## Cosa NON fa questo cantiere (fuori scope, richiede Tommaso fisicamente presente)

- Onboarding del numero di servizio nuovo (QR scan fisico).
- Accensione vera di `WA_SEND_ENABLED` e la prima campagna reale.
- Decisione commerciale/contrattuale con Primero (prezzo, timeline).
- Consulenza legale GDPR (si puo' solo preparare il materiale per l'avvocato, non sostituirla).

Questi quattro punti restano **manuali, post-piano**, elencati in fondo come "Handoff a Tommaso".

## Global Constraints

- Stesse regole non negoziabili di M1-M4 (vedi handoff 04/08): worktree isolato sempre, mai push diretto su main, `WA_SEND_ENABLED` si accende solo a mano, una sola suite pytest alla volta, contatori a DB in SQL mai read-modify-write.
- **Nessun task di questo piano tocca `D:\dev\wa-poc\profile`** ne' alcun profilo browser reale — tutti i task sono script/doc/config testabili contro DB e mock, coerente con l'incidente della notte (vedi sopra): non si ripete l'errore di mescolare pulizia infrastrutturale e dati reali.
- Riuso: `wa_optout.py` (M3, gia' condiviso), `bot_state_service.py` (M4, kill-switch per-canale), pattern contatori SQL gia' in uso in `wa_worker.py`.

---

### Task 1: Script di purge per-tenant (GDPR §12.4)

**Files:**
- Create: `backend/scripts/wa_purge_tenant.py`
- Test: `backend/tests/test_wa_purge_tenant.py`

**Interfaces:** CLI `python -m scripts.wa_purge_tenant --tenant-id <uuid> [--dry-run] [--yes]`. Cancella in ordine (rispettando le FK): `wa_inbound_events` → `wa_messages` → `wa_campaign_contacts` → `wa_campaigns` → `wa_contacts` → `wa_numbers` (con rimozione della directory/junction del profilo browser associato, se presente) → il tenant stesso. `--dry-run` stampa i conteggi senza cancellare nulla (default: NON dry-run richiede `--yes` esplicito, altrimenti abort).

- [ ] Step 1: Test che fallisce — dry-run conta senza cancellare, purge reale cancella tutto nell'ordine FK-safe, tenant sconosciuto = errore chiaro (non cancella "tutto"), `--yes` obbligatorio per la cancellazione reale.
- [ ] Step 2: Implementazione minima per far passare i test.
- [ ] Step 3: Adversarial — tenant con FK verso un altro tenant (isolamento), purge a meta' (simulare crash) non lascia stato inconsistente (transazione unica), directory profilo assente non fa fallire lo script (idempotente).
- [ ] Step 4: Reviewer dedicato + QA agent (esegue lo script contro un tenant di test in sqlite, verifica conteggi prima/dopo via query dirette).

---

### Task 2: Runbook operativo (1 pagina, per il cliente + per chi risponde a un incidente)

**Files:**
- Create: `docs/whatsapp/runbook-operativo.md`

Non e' codice — e' un documento, ma richiede comunque leggere il codice per essere accurato (non inventare procedure). Contenuto minimo (da SDD §14, Q80):
- Cosa deve/non deve fare il cliente durante una campagna attiva (non disconnettere il telefono, non rimuovere il dispositivo collegato, non rispondere a nome del bot su chat in campagna).
- Procedura di incidente: sessione WhatsApp Web persa (re-scan QR — chi lo fa, quanto tempo ci vuole, cosa succede alle campagne `running` nel frattempo: vanno in `paused` dall'health-check M2, si riprendono con `POST /wa/ops/campaigns/{id}/resume` dopo verifica manuale che il numero sia tornato `active`).
- Kill-switch: come si usa `wa_halted` per fermare TUTTO il canale in un incidente (endpoint/comando esatto, verificare in `bot_state_service.py`).
- Contatti/responsabilita': chi guarda i warning della rampa volume, chi decide se fermarla.

- [ ] Step 1: Leggere `wa_number_manager.py`, `bot_state_service.py`, l'endpoint `resume` (M3) per scrivere procedure ESATTE, non generiche.
- [ ] Step 2: Bozza scritta, revisionata da un reviewer dedicato per accuratezza tecnica (non per prosa).

---

### Task 3: Osservabilità rampa volume (10→30→60→100/gg, stop al primo warning)

**Files:**
- Investigare se esiste gia' un meccanismo di warning/soglia (grep `scrape_warning`, pattern usato sul canale Instagram in `backend/app/services/` — riuso preferito a un meccanismo nuovo).
- Create/estendi: probabile nuovo campo o vista su `wa_campaigns`/`wa_numbers` per il cap giornaliero corrente e il conteggio inviato oggi, se non gia' presente (`daily_cap` gia' esiste da M2/M3 — verificare se basta o serve un contatore "rampa" separato da riga a riga).

**Nota per chi esegue questo task**: la rampa e' un PROTOCOLLO operativo (cambiare `daily_cap` a mano ogni giorno, guardare i warning, fermarsi al primo) piu' che una feature nuova — verificare prima se `daily_cap` + i warning gia' esistenti (import/scrape hanno `scrape_warning`+Telegram, vedi PROGRESS 08/07) bastano gia', ed eventualmente estendere SOLO se manca l'osservabilità (es. un endpoint/vista che mostra "inviati oggi / cap oggi / step rampa corrente" per la UI o per chi controlla a mano).

- [ ] Step 1: Grep del codice esistente per capire cosa c'e' gia' (daily_cap, warning, Telegram) PRIMA di scrivere qualunque cosa nuova.
- [ ] Step 2: Se manca solo l'osservabilita' (probabile): task piccolo, TDD, un endpoint o una vista.
- [ ] Step 3: Se serve un meccanismo di step-rampa vero e proprio (avanzamento automatico 10→30→60→100): fermarsi e proporre il design a Tommaso prima di scrivere — decisione che merita la sua conferma esplicita (cambia il comportamento di invio in produzione), non va presa in autonomia stanotte.

---

## Handoff a Tommaso (non eseguibile stanotte)

1. **Onboarding numero di servizio nuovo**: scegliere il numero, QR scan fisico, decidere `daily_cap` iniziale (10, per la rampa).
2. **Accendere `WA_SEND_ENABLED`** sul nuovo numero, prima campagna reale di prova (non su Primero, su un contatto controllato — stesso pattern PoC-2 di M0).
3. **Parere legale GDPR**: DPA, base giuridica marketing, profilazione dal contenuto (F8, §12 SDD) — indipendente da questo piano, tempi esterni, prima si avvia meglio e'.
4. **Decisione commerciale Primero**: timeline/contratto per il canale WA (distinto da Instagram, gia' in uso).
5. **`D:\dev\wa-poc\profile` va ricreato da zero** con un nuovo QR scan (vedi incidente notte 05-06/08) prima di qualunque test dal vivo — la cartella esiste ma e' vuota.

## Riferimenti

- SDD: `docs/whatsapp/SDD-whatsapp-channel.md` §12 (GDPR), §14 (roadmap M5)
- Handoff M4→M5: `docs/superpowers/prompts/2026-08-04-whatsapp-M4-merge-M5-AVVIO.md`
- Contratto M2-M3: `docs/whatsapp/contratto-M2-M3.md`
- Incidente profilo cancellato: second-brain `sessioni/2026/2026-08-06_botoutbound-incidente-wa-poc-profile-cancellato.md`
