# Fase B WhatsApp — promozione contatti scoperti + prima campagna — piano di implementazione

Branch: `feat/wa-fase-b-promozione` (worktree `.worktrees/wa-fase-b-promozione`, da `origin/main`
dopo il merge di PR#65). Fonte: `docs/superpowers/prompts/2026-08-11-whatsapp-fase-B-promozione-AVVIO.md`
+ spec `docs/superpowers/specs/2026-08-08-wa-g2g3g5g4-e-auto-discover-design.md` §5.3-5.4.

## Vincoli globali

- **Mai promuovere un gruppo.** `wa_discovered_chats.tipo_chat == 'gruppo'` è escluso **per
  costruzione** nel servizio di promozione, non solo nella UI: anche se un id di un gruppo
  arriva nella richiesta (bug di selezione, bulk-select-all, richiesta scritta a mano), il
  backend lo scarta e riporta il motivo. Verificato sul DB condiviso l'11/08: **4** gruppi
  (non 3, il doc dell'AVVIO era indietro) hanno `numero_leggibile=true` — un numero di un
  partecipante letto dal pannello info gruppo, non il numero del gruppo.
- **`status` di `wa_discovered_chats` non torna mai indietro** (`nuovo → promosso`, mai il
  contrario): stesso vincolo già testato in Fase A (`test_una_riga_promossa_non_torna_indietro`
  — verificarne l'esistenza, il piano Fase A lo cita come test di `salvataggio.py` ma la
  garanzia qui è lato Fase B, va un test equivalente in `test_wa_promote_promozione.py`).
- **`phone_hmac`/`encrypted_phone` di `wa_discovered_chats` si riusano così come sono**, non
  si ricalcolano. `wa_discover/salvataggio.py:146-147` li scrive con `hmac_phone(riga.numero)`/
  `encrypt(riga.numero)` sullo stesso `riga.numero` in forma E.164 con `+` — identico
  contratto di `WaContact.phone_hmac`/`encrypted_phone`. Decifrare e ri-cifrare in Fase B
  sarebbe lavoro sprecato e un'occasione in più per un numero in chiaro in un log.
- **L'opt-out vince sempre**, stesso principio di `wa_ingest.py:176-179`: un `WaContact` già
  `opted_out`/`do_not_contact` non si arruola mai in una campagna nuova, nemmeno se la riga
  scoperta lo ha "ritrovato".
- **Niente numero in chiaro fuori da `crypto.decrypt`**, mai in log/report/risposta API (P12,
  stesso vincolo di `wa_contacts.lista_contatti`, che maschera sempre con `mask_phone`).
- **Ogni file nuovo ha un test dedicato**, stile Fase A: pure functions senza I/O dove
  possibile (`regole.py`), persistenza con lookup esplicita + `SAVEPOINT`/`IntegrityError`
  come ripiego sulla concorrenza (mai un INSERT lasciato parlare al vincolo).

## Decisione presa in fase di ricognizione, non nel prompt originale

Il prompt AVVIO propone **due** fix per il difetto Task 0: escludere i gruppi nella
promozione (A) *e* far smettere `pannello.numero_dal_pannello` di leggere un numero quando il
testo del pannello dice "N partecipanti" (B). **Si implementa solo A.**

Motivo, verificato in `docs/whatsapp/wa-dom-catalog.md:239-243` (misura già pagata, PoC-5): la
regex "N partecipanti/membri/iscritti" ha **recall 1/6** sul campione reale — 5 gruppi su 6
non la fanno scattare. Un guard basato su quel segnale bloccherebbe il caso raro in cui
funziona e darebbe un falso senso di sicurezza sugli altri 5/6, perché il vero problema (un
numero di un partecipante che finisce nel testo del pannello gruppo) resterebbe aperto nella
maggioranza dei casi. Il fix A basta a risolvere il problema *osservato*: le 4 righe reali con
`numero_leggibile=true` hanno **già** `tipo_chat='gruppo'` corretto — la classificazione a
monte (da titolo/segnali, non dal testo del pannello) le prende comunque. Fix B: fuori da
questo piano (vedi sezione finale).

## Struttura dei file

```
backend/app/services/wa_promote/
  __init__.py
  regole.py         Task 1 — decisione pura: promuovibile o no, e perché
  promozione.py      Task 2 — wa_discovered_chats -> WaContact, idempotente
  arruolamento.py    Task 3 — WaContact esistenti -> WaCampaignContact di una campagna
backend/app/api/
  wa_discover.py     Task 4 — GET /wa/discovered-chats, POST /wa/discovered-chats/promote
  wa_contacts.py     Task 4 — + POST /wa/contacts/enroll (esteso, non nuovo file)
frontend/lib/waApi.ts Task 5 — tipi + sezione `scoperti`, + `contatti.enroll`
frontend/app/wa/scoperti/page.tsx  Task 6 — UI di approvazione
frontend/app/wa/WaNav.tsx          Task 6 — voce nav "Scoperti"
```

## Task 1: `regole.py` — la decisione pura

```python
# backend/tests/test_wa_promote_regole.py (prima il test)
def test_gruppo_escluso_anche_con_numero():
    riga = _riga(tipo_chat="gruppo", status="nuovo", phone_hmac="x", encrypted_phone="y")
    esito = promuovibile(riga)
    assert esito.ok is False
    assert esito.motivo == "gruppo"

def test_senza_numero_escluso():
    riga = _riga(tipo_chat="individuale", status="nuovo", phone_hmac=None, encrypted_phone=None)
    assert promuovibile(riga).motivo == "senza_numero"

def test_gia_promosso_escluso_non_si_ripromuove():
    riga = _riga(tipo_chat="individuale", status="promosso", phone_hmac="x", encrypted_phone="y")
    assert promuovibile(riga).motivo == "gia_promosso"

def test_scartato_escluso():
    riga = _riga(status="scartato", ...)
    assert promuovibile(riga).motivo == "scartato"

def test_ignoto_con_numero_e_promuovibile():
    # tri-stato: "non lo so" non è "gruppo". L'operatore decide dalla UI,
    # il backend non lo blocca (a differenza del gruppo, mai bloccato).
    riga = _riga(tipo_chat="ignoto", status="nuovo", phone_hmac="x", encrypted_phone="y")
    assert promuovibile(riga).ok is True

def test_individuale_con_numero_e_promuovibile():
    ...
```

Implementazione: `dataclass DecisionePromozione(ok: bool, motivo: str | None)`, funzione
`promuovibile(riga: WaDiscoveredChat) -> DecisionePromozione`. Ordine dei controlli (il primo
che fallisce vince, un solo motivo per riga): `status != 'nuovo'` → `motivo=status stesso`
(`gia_promosso`/`scartato`) · `tipo_chat == 'gruppo'` → `motivo="gruppo"` · `phone_hmac is None
or encrypted_phone is None` → `motivo="senza_numero"` · altrimenti `ok=True`.

Nessun I/O, nessun accesso DB: prende l'oggetto già caricato (o un dataclass equivalente per i
test, per non dipendere da SQLAlchemy in `regole.py`).

## Task 2: `promozione.py` — staging → `WaContact`

```python
# backend/tests/test_wa_promote_promozione.py (prima il test)
async def test_crea_wacontact_e_segna_promosso(db, tenant, riga_individuale_con_numero):
    report = await promuovi(db, ids=[riga_individuale_con_numero.id])
    assert report.promossi == 1
    contatto = await db.scalar(select(WaContact).where(WaContact.phone_hmac == riga...phone_hmac))
    assert contatto is not None
    riga = await db.get(WaDiscoveredChat, riga_individuale_con_numero.id)
    assert riga.status == "promosso"

async def test_riusa_wacontact_esistente_stesso_hmac(db, wacontact_esistente, riga_stesso_numero):
    # dedup verso i contatti veri: la unique è (tenant_id, phone_hmac), stessa di Fase A.
    report = await promuovi(db, ids=[riga_stesso_numero.id])
    assert report.contatti_riusati == 1
    assert report.contatti_creati == 0

async def test_contatto_opted_out_si_promuove_ma_si_riporta(db, wacontact_opted_out, riga):
    # l'opt-out non impedisce "diventare WaContact" (è già un WaContact),
    # impedisce l'arruolamento in campagna (Task 3), non questo passo.
    report = await promuovi(db, ids=[riga.id])
    assert report.promossi == 1
    assert report.gia_dnc == 1

async def test_gruppo_tra_gli_id_si_scarta_gli_altri_procedono(db, riga_gruppo, riga_ok):
    report = await promuovi(db, ids=[riga_gruppo.id, riga_ok.id])
    assert report.promossi == 1
    assert report.scarti == [Scarto(id=riga_gruppo.id, motivo="gruppo")]

async def test_doppia_promozione_e_idempotente(db, riga_gia_promossa):
    report = await promuovi(db, ids=[riga_gia_promossa.id])
    assert report.scarti[0].motivo == "gia_promosso"
    # non ha ricreato un secondo WaContact

async def test_id_di_altro_tenant_si_scarta(db, riga_di_altro_tenant):
    report = await promuovi(db, ids=[riga_di_altro_tenant.id])
    assert report.scarti[0].motivo == "non_trovato"

async def test_savepoint_su_corsa_concorrente(db, monkeypatch, riga):
    # stesso schema di test_wa_ingest: due promozioni concorrenti sullo stesso
    # phone_hmac, la seconda vede IntegrityError e si ripiega sul riuso.
    ...
```

Implementazione — `async def promuovi(db, *, ids: list[str]) -> ReportPromozione`:

1. Per ogni id: `WaDiscoveredChat` by id (`db.get`). Non trovato → `Scarto(id, "non_trovato")`.
2. `promuovibile(riga)` (Task 1). Non ok → `Scarto(id, esito.motivo)`, continua.
3. Lookup `WaContact` per `(riga.tenant_id, riga.phone_hmac)`.
4. Non trovato: crea `WaContact(tenant_id=riga.tenant_id, phone_hmac=riga.phone_hmac,
   encrypted_phone=riga.encrypted_phone, display_name=riga.display_name,
   chat_title=riga.chat_title if not classifica.e_etichetta_mascherata(riga.chat_title) else None,
   first_seen_at=now)` dentro `db.begin_nested()`, `IntegrityError` → rilegge (stesso pattern
   `wa_ingest.py:141-166`). `report.contatti_creati += 1`.
5. Trovato: gap-fill `display_name` se il nuovo non è vuoto (mai cancellare). `report.contatti_riusati += 1`.
6. `riga.status = "promosso"`, `riga.updated_at = now`.
7. Se `contatto.opted_out or contatto.do_not_contact`: `report.gia_dnc += 1` (informativo, non
   blocca la promozione — blocca solo l'arruolamento, Task 3).
8. Altrimenti `report.promossi += 1`, e si accumula `contatto.id` in `report.contatti_promossi_ids`
   (serve al Task 3/frontend per proporre subito l'arruolamento senza un secondo giro di ricerca).
9. `await db.flush()` per riga, commit unico a fine batch (non per-riga: un batch di 250 id è
   una transazione sola, coerente con "innocua ri-promozione" — se fallisce a metà, niente
   scritto a metà).

`ReportPromozione`: `promossi: int`, `contatti_creati: int`, `contatti_riusati: int`,
`gia_dnc: int`, `scarti: list[Scarto(id, motivo)]`, `contatti_promossi_ids: list[str]`.

## Task 3: `arruolamento.py` — `WaContact` → `WaCampaignContact`

Stesso schema della seconda metà di `wa_ingest.ingerisci_csv` (righe 176-199), isolato in un
proprio file perché qui il contatto **esiste già** (niente normalizzazione/hash, niente CSV) —
serve anche fuori dal flusso Fase B (aggiungere contatti già noti a una nuova campagna), quindi
non va dentro `wa_promote/` in senso stretto ma il file sta comunque lì perché è la Fase B a
introdurlo per prima.

```python
# backend/tests/test_wa_promote_arruolamento.py
async def test_arruola_in_campagna_draft(db, campagna_draft, contatti):
    report = await arruola(db, campaign_id=campagna_draft.id, contact_ids=[c.id for c in contatti])
    assert report.arruolati == len(contatti)
    # next_action_at valorizzato SUBITO (invariante I3, mai NULL su riga non terminale)

async def test_campagna_non_draft_rifiutata(db, campagna_running, contatti):
    with pytest.raises(CampagnaNonModificabile):
        await arruola(db, campaign_id=campagna_running.id, contact_ids=[...])

async def test_contatto_opted_out_escluso(db, campagna_draft, contatto_opted_out):
    report = await arruola(db, campaign_id=campagna_draft.id, contact_ids=[contatto_opted_out.id])
    assert report.gia_dnc == 1
    assert report.arruolati == 0

async def test_gia_arruolato_non_duplica(db, campagna_draft, contatto_gia_in_campagna):
    report = await arruola(db, campaign_id=campagna_draft.id, contact_ids=[contatto_gia_in_campagna.id])
    assert report.gia_presenti == 1

async def test_total_contacts_aggiornato_senza_read_modify_write(db, ...):
    # stesso vincolo di wa_contacts.rimuovi_contatto:139 — UPDATE ... SET total_contacts = total_contacts + N
```

`async def arruola(db, *, campaign_id: str, contact_ids: list[str]) -> ReportArruolamento`.
Solleva `CampagnaNonModificabile` se `campagna.status != draft` (l'API la traduce in 409,
stesso messaggio di `wa_contacts.ingest`). Per ogni `contact_id`: lookup `WaContact` (non
trovato → scarto "contatto_inesistente"); opted_out/do_not_contact → `gia_dnc`; lookup
`WaCampaignContact(campaign_id, contact_id)` esistente → `gia_presenti`; altrimenti crea con
`status=queued, current_step=-1, next_action_at=now, failure_count=0`. A fine batch:
`UPDATE wa_campaigns SET total_contacts = total_contacts + :n` (non un `SELECT count(*)` come
in `wa_ingest.py:196-199` — lì è giustificato perché l'ingest può correre più volte sullo
stesso file, qui il batch è sempre incrementale su id nuovi, l'`UPDATE` diretto evita la
race che l'altro endpoint (`rimuovi_contatto`) già paga con un commento dedicato).

## Task 4: API

`backend/app/api/wa_discover.py` (nuovo router, `prefix="/wa/discovered-chats"`, registrato in
`main.py` accanto agli altri router `wa_*`):

- `GET ""` — query params `number_id` (required), `status` (default `"nuovo"`), `tipo_chat`
  (opzionale, filtro), `ha_numero` (opzionale bool). Risposta: lista di righe **mascherate**
  (`numero_mascherato = mask_phone(decrypt(encrypted_phone)) if encrypted_phone else None`,
  mai il numero intero — stesso vincolo di `wa_contacts.lista_contatti`). `limit`/`offset`
  come `wa_contacts`.
- `POST "/promote"` — body `{"ids": list[str]}`. Chiama `wa_promote.promozione.promuovi`.
  Nessun controllo di stato campagna qui: la promozione non tocca campagne. Risposta: il
  `ReportPromozione` serializzato (stesso stile di `ingest`: dict con le stesse chiavi del
  dataclass, `scarti` come lista di dict).

`backend/app/api/wa_contacts.py` — nuovo endpoint aggiunto (non nuovo file):

- `POST "/enroll"` — body `{"campaign_id": str, "contact_ids": list[str]}`. Stesso guard 409
  di `ingest` se la campagna non è `draft` (`CampagnaNonModificabile` → `HTTPException(409, ...)`).
  Risposta: `ReportArruolamento` serializzato.

Test API (`test_wa_discover_api.py`, `test_wa_contacts_api.py` esteso): 404 su `number_id`
inesistente, 422 su body malformato, 409 su enroll verso campagna non-draft, che i gruppi
tornino nella lista GET (l'operatore deve poterli VEDERE anche se non promuovibili — solo la
promozione li blocca, non la visibilità) ma con un campo che li segnala come non promuovibili
lato UI (es. `promuovibile: bool` calcolato riusando `regole.promuovibile` anche nel
serializzatore GET, per non duplicare la regola in due posti).

## Task 5: `frontend/lib/waApi.ts`

Aggiungere, seguendo lo stile esistente (tipi presi 1:1 dal backend, commento che cita il
serializzatore di origine):

```ts
export type WaDiscoveredChat = {
  id: string
  chat_title: string | null
  display_name: string | null
  tipo_chat: 'individuale' | 'gruppo' | 'ignoto'
  numero_leggibile: boolean
  numero_mascherato: string | null
  status: 'nuovo' | 'promosso' | 'scartato'
  promuovibile: boolean
  discovered_at: string | null
}

export type ScartoPromozione = { id: string; motivo: string }
export type ReportPromozione = {
  promossi: number; contatti_creati: number; contatti_riusati: number
  gia_dnc: number; scarti: ScartoPromozione[]; contatti_promossi_ids: string[]
}
export type ReportArruolamento = {
  arruolati: number; gia_presenti: number; gia_dnc: number
  scarti: { id: string; motivo: string }[]
}
```

Sezione `waApi.scoperti = { list(numberId, filtri), promote(ids) }` +
`waApi.contatti.enroll(campaignId, contactIds)`.

## Task 6: `frontend/app/wa/scoperti/page.tsx`

Prima di scrivere, leggere `frontend/app/wa/campagne/[id]/page.tsx` e
`frontend/app/wa/numeri/page.tsx` per lo stile di tabella/filtri già in uso in questo cantiere
(non inventare un pattern nuovo). Contenuto:

- Selettore numero (riusa lo stesso pattern di selezione già presente per `wa_number_id` altrove).
- Filtri: `tipo_chat` (default: tutti tranne implicito — i gruppi restano visibili ma marcati
  "non promuovibile", mai nascosti: l'operatore deve poter vedere cosa lo scan ha trovato),
  `status` (default `nuovo`), `ha_numero`.
- Tabella con checkbox per riga, colonna `numero_mascherato`, colonna `tipo_chat` (badge),
  riga di gruppo visivamente disabilitata per la checkbox (coerente col blocco lato backend —
  **non impedire il click**, impedire l'esito: se per un bug la checkbox restasse abilitata,
  il backend scarta comunque quell'id).
- Bottone "Promuovi selezionati" → `waApi.scoperti.promote(ids)`, mostra il report (creati,
  riusati, scarti con motivo) — stesso stile di feedback di `ReportIngest` in
  `campagne/[id]/page.tsx` (verificare come quella pagina mostra `ReportIngest` oggi e ripetere
  il pattern).
- Dopo una promozione riuscita: selettore campagna (solo campagne `draft` dello stesso
  `wa_number_id` — filtrare client-side su `waApi.campagne.list()`) + bottone "Aggiungi alla
  campagna" → `waApi.contatti.enroll(campaignId, report.contatti_promossi_ids)`. Se nessuna
  campagna `draft` esiste per quel numero, link a `/wa/campagne/nuova`.

`frontend/app/wa/WaNav.tsx` — nuova voce "Scoperti" (`/wa/scoperti`), tra "Campagne" e "Numeri"
o dove lo stile esistente suggerisce.

## Chiusura del modulo (obbligatoria, skill `sviluppo-modulo`)

1. Reviewer dedicato per ogni task (Task 1→6, mai in parallelo sull'implementazione).
2. QA agent per funzione: test unit/integration + almeno un giro E2E reale da browser
   (Playwright, `PLAYWRIGHT_BROWSERS_PATH=D:\dev\.playwright-browsers`) sulla pagina
   `/wa/scoperti` contro il DB condiviso (i 273 record reali di Primero già in staging — non
   servono fixture sintetiche per il giro E2E, i dati veri ci sono già).
3. Lista test manuali UI — minimo 20, eseguiti dal QA agent via browser.
4. Lista test ADVERSARIAL — minimo 30. Categorie obbligatorie per QUESTO modulo, oltre alle
   generali dello skill: **promuovere due volte lo stesso id in `Promise.all`** (race reale,
   non sequenziale) · **id di un gruppo iniettato a mano nel body POST** (bypassare la UI) ·
   **id misto tenant diversi nello stesso batch** · **enroll verso una campagna che passa a
   `running` fra la GET e la POST** (TOCTOU) · **batch di 250+ id** (volume) · **phone_hmac
   collidente fra due righe scoperte diverse per un urto teorico** (fuori scope se già coperto
   da Fase A, verificare) · **contatto promosso poi manualmente opted-out fra promozione e
   arruolamento**.
5. Invariante di dominio verificata via SQL a fine giro: **nessuna riga `wa_discovered_chats`
   con `tipo_chat='gruppo'` ha mai `status='promosso'`** (query diretta sul DB condiviso dopo
   il collaudo, non solo sui dati di test).
6. Fix loop fino al 100%, poi final whole-branch review (`superpowers:requesting-code-review`).
7. Collaudo manuale di Tommaso: NON qui (non è MVP), si logga stato e si passa oltre.
8. Salva le liste in `.superpowers/sdd/qa-wa-fase-b-tests.md` e `qa-wa-fase-b-adversarial.md`
   nel worktree (modelli: `d:\dev\thevista-app-magazzino\.superpowers\sdd\qa-50-tests.md`).
9. A fine modulo: aggiorna `PROGRESS.md`, `INDEX.md` se cambia la mappa, sezione datata in
   `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` (per CLAUDE.md
   locale di questa repo).

## Fuori da questo piano, con motivo

- **Fix B del difetto Task 0** (guard su "N partecipanti" nel testo del pannello): recall 1/6
  misurato, vedi sezione "Decisione presa in fase di ricognizione". Il fix A (esclusione
  gruppi per costruzione in `regole.py`) copre il problema osservato. Se in futuro emergessero
  righe individuali con un numero sbagliato per un motivo diverso dal gruppo, va aperto un
  nuovo giro di misura, non questo fix.
- **Azione "scarta" (`status='scartato'`) dalla UI**: lo schema la supporta, il prompt AVVIO
  non la richiede ("selezionare È l'approvazione" — non selezionare lascia `nuovo`, non
  richiede uno scarto esplicito). Aggiungerla ora sarebbe UI per un caso d'uso non ancora
  chiesto.
- **Sostituire la quarantena d'invio a timer cieco con la soglia di sincronizzazione**: già
  segnalato nel prompt AVVIO come lavoro separato, tocca il percorso d'invio di M3, merita la
  sua PR.
- **Filtro per Lista/etichetta WhatsApp**: scartato come base (decisione già presa, vedi AVVIO
  prompt) — Primero non ha Liste, il campo `source_filtro` resta pronto ma inerte.
