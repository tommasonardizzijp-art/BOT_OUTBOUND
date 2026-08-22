# Guardia "AI senza bio" — Implementation Plan

> ## ⛔ SUPERATO IN PARTE — leggi prima questo
>
> La **regola** di questo piano non è più quella giusta. Qui la guardia è calibrata
> stretta su `source_type='scrape'`, perché quando è stato scritto la bio su import
> arrivava sempre e su scrape mai.
>
> Nella stessa sessione (22/08/2026) la decisione è cambiata, dopo aver stabilito che
> lo username diventa una chiave d'identità di prima classe. **La regola finale è una
> sola condizione, senza eccezioni per sorgente:**
>
> > AI accesa → «Solo DM» spento. AI spenta → tutte e tre le opzioni, su tutte le sorgenti.
>
> **Piano valido: [`2026-08-22-username-chiave-di-prima-classe.md`](2026-08-22-username-chiave-di-prima-classe.md).**
> La guardia del pulsante è la sua **Task 7**, che rimanda alle Task 1-3 di questo
> documento per il codice — **con `valida_ai_senza_bio` a due argomenti**
> (`ai_enabled`, `enrichment_level`), senza `source_type`.
>
> Cosa resta buono qui: la struttura dei task, l'harness dei test HTTP (Task 2, Step 1a)
> e le trasformazioni della UI (Task 3). Cosa NON va copiato: la firma a tre argomenti,
> la condizione su `import`, e i due test che asseriscono «su import resta permesso».

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Impedire di configurare una campagna con «Personalizza con AI» attivo e livello di arricchimento «Solo DM» nei casi in cui la bio non arriva mai, spiegando il perche' invece di lasciar produrre DM finto-personalizzati.

**Architecture:** Un predicato puro in `app/models/campaign.py`, chiamato dai DUE punti che gia' validano la combinazione finale dei motori (create e update di `campaigns.py`), piu' il gemello TypeScript in un helper unico consumato dalle due pagine che mostrano il selettore. Il backend resta l'autorita' (risponde 400); il frontend disabilita il pulsante e dice perche', cosi' l'errore non arriva a sorpresa.

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic (backend, `backend/venv`), pytest, Next.js 15 + TypeScript + Tailwind (frontend).

**Spec:** questo documento. Nasce dalla sessione del 22/08/2026: vedi la sezione «Diagnosi verificata» sotto, che riporta i fatti con file e riga.

---

## Global Constraints

- Branch dedicato + PR, mai push diretto su `main` (CLAUDE.md della repo).
- `loguru`, mai `print()`.
- Python: `backend/venv/Scripts/python.exe`. Test: `venv/Scripts/python.exe -m pytest`.
- Frontend senza test runner (verificato: `package.json` ha solo `dev/build/start/lint`). La verifica frontend e' `npx tsc --noEmit` + i controlli manuali della Task 4. **Non installare un test runner per questo lavoro.**
- Una suite pytest alla volta (sqlite condiviso, vedi memory `botoutbound-una-suite-pytest-alla-volta`).
- Test proporzionati al piano: questa e' una guardia da due file, non un modulo. Il protocollo da 20 manuali + 30 adversarial di `sviluppo-modulo` NON si applica qui; la Task 4 elenca i controlli manuali che servono davvero.

---

## Diagnosi verificata (perche' la regola e' questa e non un'altra)

Il livello «Solo DM» (`enrichment_level='none'`) **non impedisce la visita al profilo**: decide solo se partono le richieste dedicate a email/telefono. Cio' che cambia da caso a caso e' se la **bio** arriva lo stesso.

| Sorgente | La bio arriva? | Dove si vede |
|---|---|---|
| `import` (motore API) | **Si', sempre** — salvata senza guardare il livello | `app/services/import_resolver.py:246,252,273` |
| `import` (motore browser) | **Si', sempre** | `app/services/browser_import.py:170,197` |
| `scrape` | **No** — la Fase Lista salva `full_name` ma nessuna `biography`, e la Fase Bio e' spenta dal livello | `app/services/scrape_list.py:199-203`, `app/services/scrape_bios.py:82` |

Quindi la combinazione davvero incoerente e' **una sola**:

```
source_type = 'scrape'  +  ai_enabled = True  +  enrichment_level = 'none'
   -> il follower arriva alla fase DM con biography = NULL
   -> _build_user_prompt scrive "Bio Instagram: (bio vuota)"
   -> l'AI applica la regola 10 del system prompt ("se la bio e' vuota,
      copia il template quasi invariato")
   -> si spende una chiamata AI per riottenere il template
```

Su `import` la stessa combinazione **funziona**: la risoluzione apre il profilo e la bio e' gia' in DB quando l'AI genera. Vietarla li' toglierebbe una configurazione sana.

### Decisione aperta per Tommaso

Il piano implementa la **calibratura stretta (A)**. Se preferisci la regola letterale (B), e' una riga in meno — indicata nella Task 1, Step 3.

- **(A) consigliata — blocca solo `scrape`.** Vieti esattamente il caso rotto. Le campagne import restano libere di stare su «Solo DM» con l'AI accesa, come sono oggi.
- **(B) letterale — blocca AI + «Solo DM» ovunque.** Regola piu' semplice da spiegare, ma vieta una configurazione che su import funziona. Se la scegli, cambia solo il messaggio d'errore (togli il riferimento alla Fase Bio) e togli la condizione su `source_type`.

### Cosa questo piano NON fa (dichiarato, non dimenticato)

- **Non riduce le visite al profilo.** Su import restano due (risoluzione + invio). Il percorso a visita unica e' stato valutato e messo da parte in questa sessione.
- **Non tocca le campagne gia' esistenti** in quello stato. La guardia vive su create/update: una campagna gia' salvata cosi' continua a girare. Bloccarla a runtime spegnerebbe in silenzio un lavoro in corso. Per trovarle, query nella Task 5.

---

## File Structure

| File | Responsabilita' | Azione |
|---|---|---|
| `backend/app/models/campaign.py` | Il predicato e il messaggio d'errore. Sta qui accanto a `contatti_richiesti_dal_livello`: stessa famiglia (predicati puri sui campi campagna), nessuna dipendenza da FastAPI. | Modifica |
| `backend/app/api/campaigns.py` | Chiama il predicato nei due punti che gia' validano la combinazione finale (create :222, update :361) e traduce in HTTP 400. | Modifica |
| `backend/tests/test_guardia_ai_senza_bio.py` | Copre il predicato e i due verbi HTTP, in entrambe le direzioni. | Crea |
| `frontend/lib/arricchimento.ts` | Gemello TS della regola + testo mostrato. Unico posto sul frontend: due copie divergerebbero. | Crea |
| `frontend/app/campaigns/new/page.tsx` | Disabilita «Solo DM» in creazione. | Modifica |
| `frontend/app/campaigns/[id]/page.tsx` | Disabilita «Solo DM» nel dettaglio. | Modifica |

---

### Task 1: Il predicato nel modello

**Files:**
- Modify: `backend/app/models/campaign.py` (dopo `contatti_richiesti_dal_livello`, ~riga 112)
- Test: `backend/tests/test_guardia_ai_senza_bio.py`

**Interfaces:**
- Produces: `valida_ai_senza_bio(source_type: str, ai_enabled: bool, enrichment_level: str) -> str | None` — ritorna il messaggio d'errore, oppure `None` se la combinazione e' valida. Stessa forma di `valida_combinazione_motori` in `app/services/inbox_browser/gate.py`, cosi' i due gate si chiamano allo stesso modo.

- [ ] **Step 1: Scrivi il test che fallisce**

Crea `backend/tests/test_guardia_ai_senza_bio.py`:

```python
"""Guardia: AI accesa + livello 'none' dove la bio non arriva mai.

Perche' esiste: su una campagna 'scrape' il livello 'none' spegne la Fase Bio
(scrape_bios.py:82) e la Fase Lista non salva la biography (scrape_list.py:199-203),
quindi l'AI genera su "(bio vuota)" e la regola 10 del system prompt le fa ricopiare
il template. Si paga una chiamata AI per riottenere il testo di partenza.

Su 'import' NO: la risoluzione salva la bio a prescindere dal livello
(import_resolver.py:246 / browser_import.py:170), quindi la combinazione e' sana
e non va vietata.
"""
import pytest

from app.models.campaign import valida_ai_senza_bio


def test_scrape_ai_e_none_e_vietata():
    errore = valida_ai_senza_bio("scrape", True, "none")
    assert errore is not None
    # Deve dire cosa fare, non solo che e' vietato.
    assert "Fase Bio" in errore
    assert "bio" in errore.lower()


@pytest.mark.parametrize("livello", ["bio", "contacts"])
def test_scrape_ai_con_arricchimento_e_permessa(livello):
    assert valida_ai_senza_bio("scrape", True, livello) is None


def test_scrape_senza_ai_e_none_e_permessa():
    # Modalita' template (es. la campagna DM di Primero adv3): nessuna bio serve.
    assert valida_ai_senza_bio("scrape", False, "none") is None


def test_import_ai_e_none_e_permessa():
    # La risoluzione apre il profilo e salva la bio a prescindere dal livello:
    # l'AI ha il dato. Vietarlo qui toglierebbe una configurazione sana.
    assert valida_ai_senza_bio("import", True, "none") is None


def test_import_senza_ai_e_none_e_permessa():
    assert valida_ai_senza_bio("import", False, "none") is None
```

- [ ] **Step 2: Esegui il test e verifica che fallisca**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_guardia_ai_senza_bio.py -v`
Expected: FAIL — `ImportError: cannot import name 'valida_ai_senza_bio'`

- [ ] **Step 3: Scrivi l'implementazione minima**

In `backend/app/models/campaign.py`, subito dopo la fine di `contatti_richiesti_dal_livello` (riga 111, `return livello is None or livello == ENRICHMENT_CONTACTS`) e prima di `class Campaign(Base):`:

```python
def valida_ai_senza_bio(source_type: str, ai_enabled: bool, enrichment_level: str) -> str | None:
    """Messaggio d'errore se l'AI dovrebbe personalizzare senza avere la bio, altrimenti None.

    Stessa forma di `valida_combinazione_motori` (services/inbox_browser/gate.py):
    ritorna la stringa da mettere nel 400, non solleva.

    Il vincolo NON e' "AI + livello 'none'": e' "AI + nessuna bio in arrivo", e le due
    cose coincidono su una sola sorgente. Su 'import' la bio si salva a prescindere dal
    livello (import_resolver.py:246, browser_import.py:170), quindi l'AI ha il dato e la
    combinazione e' sana. Su 'scrape' invece la Fase Lista salva solo full_name
    (scrape_list.py:199-203) e il livello 'none' spegne la Fase Bio (scrape_bios.py:82):
    il follower arriva alla generazione con biography=NULL, `_build_user_prompt` scrive
    "(bio vuota)" e la regola 10 del system prompt fa ricopiare il template. Si spende
    una chiamata AI per riottenere il punto di partenza.

    Perche' un divieto e non un avviso: il DM esce plausibile, quindi il difetto non si
    vede ne' nei log ne' nel messaggio inviato. Si nota solo dal conto delle chiamate AI.
    """
    if not ai_enabled:
        return None
    if enrichment_level != ENRICHMENT_NONE:
        return None
    # (B) regola letterale: cancella le due righe qui sotto per vietare la
    # combinazione anche sulle campagne import.
    if source_type == "import":
        return None
    return (
        "Con la personalizzazione AI attiva il livello «Solo DM» non ha dati su cui "
        "lavorare: su una campagna che raccoglie da una pagina target la bio arriva "
        "solo dalla Fase Bio, che questo livello spegne. Alza il livello a «Bio» o "
        "«Bio + contatti», oppure spegni la personalizzazione AI e usa i template."
    )
```

- [ ] **Step 4: Esegui i test e verifica che passino**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_guardia_ai_senza_bio.py -v`
Expected: PASS (6 test)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/campaign.py backend/tests/test_guardia_ai_senza_bio.py
git commit -m "feat(campagne): predicato che vieta l'AI dove la bio non arriva mai"
```

---

### Task 2: Il gate sui due verbi HTTP

**Files:**
- Modify: `backend/app/api/campaigns.py` (create ~riga 222, update ~riga 361)
- Test: `backend/tests/test_guardia_ai_senza_bio.py` (si aggiunge alla stessa suite)

**Interfaces:**
- Consumes: `valida_ai_senza_bio(source_type, ai_enabled, enrichment_level) -> str | None` dalla Task 1.
- Produces: `POST /campaigns` e `PATCH /campaigns/{id}` rispondono `400` con `detail` uguale al messaggio del predicato.

**Nota per chi implementa — leggi prima di scrivere:** la guardia va messa **dopo** che tutti i campi sono stati applicati sull'oggetto `campaign`, accanto al `valida_combinazione_motori` esistente. Nell'update i tre campi rilevanti sono gia' applicati prima di quel punto (`ai_enabled` a :322-323, `enrichment_level` a :359), quindi lo stesso controllo copre **entrambe le direzioni**: chi alza l'AI su una campagna gia' a «Solo DM» e chi abbassa il livello su una campagna gia' con AI. Un controllo su un campo alla volta lascerebbe passare la direzione non controllata — e' lo stesso motivo scritto nel commento a :356-360.

- [ ] **Step 1a: Copia l'harness HTTP dal file fratello**

**Non inventare il setup.** Le route campagne sono dietro autenticazione (`app/main.py:121`, `dependencies=_protected`) e vogliono un DB: un `TestClient(app)` nudo fallisce su entrambi. L'harness gia' collaudato e' in `backend/tests/test_enrichment_level_api.py:92-151`.

Copia **verbatim** in `backend/tests/test_guardia_ai_senza_bio.py`, sotto ai test della Task 1:
- il blocco di import delle tabelle ORM (`test_enrichment_level_api.py:19-33`) — servono tutte, altrimenti `Base.metadata.create_all` non crea le FK;
- la fixture `_temp_db` (`:95-118`);
- la fixture `client` (`:120-151`).

Nella fixture `client`, **cambia l'id dell'utente finto**: usa `"00000000-0000-0000-0000-000000000005"` invece di `...0004`. Due moduli con lo stesso id su DB temporanei separati non collidono oggi, ma e' l'assunzione che si rompe da sola il giorno che qualcuno unisce i due file.

Fatti confermati, non da riverificare: `POST /api/campaigns` risponde **201** (`campaigns.py:189`), il prefisso e' `/api` + `/campaigns`.

- [ ] **Step 1b: Scrivi i test che falliscono**

Aggiungi in fondo allo stesso file:

```python
# -- I due verbi HTTP -------------------------------------------------------
# Entrambe le direzioni del PATCH, non una: il gate sta a valle dei campi
# applicati, quindi deve fermare sia "accendo l'AI su una campagna gia' 'none'"
# sia "abbasso il livello su una campagna che ha gia' l'AI". Un controllo su un
# campo alla volta lascerebbe passare la direzione non controllata.

def _crea(client, **override):
    corpo = {
        "name": "guardia-test",
        "source_type": "scrape",
        "target_username": "un_target",
        "base_message_template": "Ciao, ti va di sentirci?",
        "ai_enabled": False,
        "enrichment_level": "bio",
    }
    corpo.update(override)
    return client.post("/api/campaigns", json=corpo)


def test_create_rifiuta_scrape_ai_e_none(client):
    r = _crea(client, name="g-create", ai_enabled=True, enrichment_level="none")
    assert r.status_code == 400, r.text
    assert "Solo DM" in r.json()["detail"]


def test_patch_accendere_ai_su_campagna_none_e_rifiutato(client):
    r = _crea(client, name="g-patch-ai", ai_enabled=False, enrichment_level="none")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.patch(f"/api/campaigns/{cid}", json={"ai_enabled": True})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]


def test_patch_abbassare_livello_su_campagna_ai_e_rifiutato(client):
    r = _crea(client, name="g-patch-liv", ai_enabled=True, enrichment_level="bio")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.patch(f"/api/campaigns/{cid}", json={"enrichment_level": "none"})
    assert p.status_code == 400, p.text
    assert "Solo DM" in p.json()["detail"]


def test_patch_import_ai_e_none_resta_permesso(client):
    # La calibratura (A) in una riga: su import la bio arriva comunque, quindi
    # questa combinazione NON va vietata. Se un giorno passi alla regola (B),
    # questo test va cambiato di proposito, non "aggiustato" perche' e' rosso.
    r = _crea(client, name="g-import", source_type="import", target_username=None,
              ai_enabled=True, enrichment_level="bio")
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    p = client.patch(f"/api/campaigns/{cid}", json={"enrichment_level": "none"})
    assert p.status_code == 200, p.text
```

**Se un create risponde 422:** lo schema vuole un campo che il payload non manda. Leggi `CampaignCreate` in `backend/app/schemas/campaign.py` e aggiungilo a `_crea` — non allentare lo schema.

- [ ] **Step 2: Esegui i test e verifica che falliscano**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_guardia_ai_senza_bio.py -v -k "create or patch"`
Expected: FAIL — i quattro test tornano 200/201 dove ne aspettano 400

- [ ] **Step 3: Aggiungi il gate nel create**

In `backend/app/api/campaigns.py`, importa il predicato accanto agli altri import da `app.models.campaign`, poi subito **dopo** il blocco esistente:

```python
    errore_motori = valida_combinazione_motori(
        campaign.inbox_engine, campaign.bio_engine, campaign.enrichment_level,
    )
    if errore_motori:
        raise HTTPException(status_code=400, detail=errore_motori)
```

aggiungi:

```python
    errore_ai = valida_ai_senza_bio(
        campaign.source_type, campaign.ai_enabled, campaign.enrichment_level,
    )
    if errore_ai:
        raise HTTPException(status_code=400, detail=errore_ai)
```

- [ ] **Step 4: Aggiungi lo stesso gate nell'update**

Nello stesso file, nella `update_campaign`, subito dopo il `valida_combinazione_motori` dell'update (~riga 361-365), aggiungi lo stesso blocco di tre righe dello Step 3, identico. Deve stare **dopo** entrambi i campi applicati, non prima.

- [ ] **Step 5: Esegui tutta la suite e verifica che passi**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_guardia_ai_senza_bio.py -v`
Expected: PASS (10 test)

- [ ] **Step 6: Verifica che i test siano rossi senza il fix**

Commenta i due blocchi aggiunti (create e update), rilancia la suite: i quattro test HTTP DEVONO tornare rossi. Poi rimettili. Un test che resta verde con la guardia spenta non sta misurando la guardia.

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_guardia_ai_senza_bio.py -v`

- [ ] **Step 7: Verifica di non aver rotto le suite vicine**

Run: `cd backend && venv/Scripts/python.exe -m pytest tests/test_campaign_messaging_toggle.py tests/test_operator_guardrails.py -q`
Expected: PASS. Se una fixture creava campagne `scrape` + AI + `none`, ora prende 400: e' la guardia che funziona — sistema la fixture, non la guardia.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/campaigns.py backend/tests/test_guardia_ai_senza_bio.py
git commit -m "feat(api): create e patch rifiutano l'AI senza bio, in entrambe le direzioni"
```

---

### Task 3: Il pulsante spento, con la spiegazione

**Files:**
- Create: `frontend/lib/arricchimento.ts`
- Modify: `frontend/app/campaigns/new/page.tsx:507-534`
- Modify: `frontend/app/campaigns/[id]/page.tsx:1117-1135`

**Interfaces:**
- Produces: `soloDmVietato(sourceType: 'scrape' | 'import', aiEnabled: boolean): boolean` e `MOTIVO_SOLO_DM_VIETATO: string`.

**Nota:** il backend resta l'autorita' — questo serve a non far arrivare l'errore a sorpresa dopo aver compilato il form. La regola e' scritta due volte (Python e TS): tenerla in **un solo** file per lato e' cio' che impedisce alle due copie di divergere in silenzio.

- [ ] **Step 1: Crea l'helper**

Crea `frontend/lib/arricchimento.ts`:

```ts
// Gemello TypeScript della guardia backend `valida_ai_senza_bio`
// (backend/app/models/campaign.py). Il backend resta l'autorita': risponde 400 comunque.
// Questo serve solo a spegnere il pulsante e dire perche', invece di far scoprire il
// divieto dopo aver compilato il form.
//
// Se cambi la regola qui, cambiala anche di la': sono due copie della stessa decisione.

/**
 * Il livello «Solo DM» e' incompatibile con la personalizzazione AI solo dove la bio
 * non arriva mai — cioe' sulle campagne che raccolgono da una pagina target. Su una
 * lista importata la risoluzione salva la bio a prescindere dal livello, quindi l'AI
 * ha il dato e la combinazione e' sana.
 */
export function soloDmVietato(
  sourceType: 'scrape' | 'import',
  aiEnabled: boolean,
): boolean {
  return aiEnabled && sourceType === 'scrape'
}

export const MOTIVO_SOLO_DM_VIETATO =
  'Con la personalizzazione AI attiva questo livello non ha dati su cui lavorare: '
  + 'la bio arriva solo dalla Fase Bio, che «Solo DM» spegne. Alza il livello a «Bio», '
  + 'oppure spegni la personalizzazione AI e usa i template.'
```

- [ ] **Step 2: Applica alla pagina di creazione**

In `frontend/app/campaigns/new/page.tsx`, aggiungi l'import in cima:

```ts
import { soloDmVietato, MOTIVO_SOLO_DM_VIETATO } from '@/lib/arricchimento'
```

Nel blocco del selettore (righe 507-524), sostituisci il `.map` con questa versione — la differenza e' `vietato`, `disabled`, `title` e il `return` anticipato che impedisce di selezionare un livello spento:

```tsx
                ] as const).map(({ v, t, d }) => {
                  const vietato = v === 'none' && soloDmVietato(sourceType, form.ai_enabled)
                  return (
                  <button
                    key={v}
                    type="button"
                    disabled={vietato}
                    title={vietato ? MOTIVO_SOLO_DM_VIETATO : d}
                    onClick={() => { if (!vietato) setEnrichmentLevel(v) }}
                    className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${
                      vietato
                        ? 'bg-gray-800/40 border-gray-800 text-gray-600 cursor-not-allowed'
                        : enrichmentLevel === v
                        ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                        : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                    }`}
                  >
                    {t}
                    <span className="block text-xs font-normal mt-0.5 opacity-70">{d}</span>
                  </button>
                  )
                })}
```

Subito sotto il `</div>` che chiude `flex gap-3`, prima del blocco `{enrichmentLevel === 'none' && (`, aggiungi la spiegazione sempre visibile quando il divieto e' attivo:

```tsx
              {soloDmVietato(sourceType, form.ai_enabled) && (
                <p className="text-xs text-gray-500">{MOTIVO_SOLO_DM_VIETATO}</p>
              )}
```

- [ ] **Step 3: Chiudi la porta di servizio della creazione**

Ancora in `new/page.tsx`: accendere l'AI **dopo** aver scelto «Solo DM» lascerebbe lo state su `'none'` e il form partirebbe verso un 400. Nell'handler del toggle AI (riga 417) riporta il livello a `'bio'` quando il divieto scatta:

```tsx
                onClick={() => setForm(f => {
                  const nuovo = { ...f, ai_enabled: !f.ai_enabled }
                  // Accendere l'AI mentre il livello e' 'none' porterebbe il form dritto
                  // in un 400: si alza il livello qui, invece di far fallire l'invio.
                  if (soloDmVietato(sourceType, nuovo.ai_enabled) && enrichmentLevel === 'none') {
                    setEnrichmentLevel('bio')
                  }
                  return nuovo
                })}
```

- [ ] **Step 4: Applica alla pagina di dettaglio**

In `frontend/app/campaigns/[id]/page.tsx`, aggiungi lo stesso import, poi nel selettore (righe 1117-1135) applica la stessa trasformazione. Qui il `disabled` esistente va **combinato**, non sostituito, e i valori vengono dalla campagna:

```tsx
            ] as const).map(({ v, t, d }) => {
              const vietato = v === 'none' && soloDmVietato(campaign.source_type, campaign.ai_enabled)
              return (
              <button
                key={v}
                type="button"
                disabled={switchingEnrichment || vietato}
                title={vietato ? MOTIVO_SOLO_DM_VIETATO : d}
                onClick={() => { if (!vietato) handleEnrichmentSwitch(v) }}
                className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors disabled:opacity-50 ${
                  vietato
                    ? 'bg-gray-800/40 border-gray-800 text-gray-600 cursor-not-allowed'
                    : (campaign.enrichment_level ?? 'none') === v
                    ? 'bg-purple-600/20 border-purple-500 text-purple-300'
                    : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                }`}
              >
                {t}
                <span className="block text-xs font-normal mt-0.5 opacity-70">{d}</span>
              </button>
              )
            })}
```

E la spiegazione, subito dopo il `</div>` che chiude `flex gap-3`:

```tsx
          {soloDmVietato(campaign.source_type, campaign.ai_enabled) && (
            <p className="text-xs text-gray-500">{MOTIVO_SOLO_DM_VIETATO}</p>
          )}
```

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: nessun errore. Se `campaign.ai_enabled` risulta possibilmente `undefined`, usa `campaign.ai_enabled ?? false` — non allargare il tipo dell'helper.

- [ ] **Step 6: Build**

Run: `cd frontend && npm run build`
Expected: build completata.

- [ ] **Step 7: Commit**

```bash
git add frontend/lib/arricchimento.ts frontend/app/campaigns/new/page.tsx "frontend/app/campaigns/[id]/page.tsx"
git commit -m "feat(ui): Solo DM spento e spiegato quando l'AI non avrebbe la bio"
```

---

### Task 4: Controlli manuali dalla UI

**Files:** nessuno — verifica.

Avvia backend e frontend (`start.bat`) e fai questi otto passaggi. Sono proporzionati alla dimensione della modifica: il grosso del comportamento e' gia' inchiodato dai test della Task 2.

- [ ] **Step 1:** Nuova campagna, sorgente **Pagina target**, AI **spenta** → «Solo DM» e' cliccabile e selezionabile. *(La campagna DM di Primero adv3 deve restare configurabile: e' il caso che Tommaso ha chiesto esplicitamente di non rompere.)*
- [ ] **Step 2:** Stessa schermata, accendi **Personalizza con AI** → «Solo DM» diventa grigio e non cliccabile, compare la spiegazione sotto ai tre pulsanti.
- [ ] **Step 3:** Seleziona «Solo DM» con AI spenta, **poi** accendi l'AI → il livello si sposta da solo su «Bio» (Step 3 della Task 3), il form non resta in uno stato che verrebbe rifiutato.
- [ ] **Step 4:** Spegni di nuovo l'AI → «Solo DM» torna cliccabile.
- [ ] **Step 5:** Nuova campagna, sorgente **Lista importata**, AI **accesa** → «Solo DM» resta **cliccabile**. E' la calibratura (A): su import non si vieta nulla.
- [ ] **Step 6:** Dettaglio di una campagna scrape ferma con AI accesa → «Solo DM» grigio con la spiegazione; gli altri due livelli si cambiano ancora.
- [ ] **Step 7:** Dettaglio di una campagna import con AI accesa → i tre livelli si cambiano tutti, «Solo DM» compreso.
- [ ] **Step 8:** Con la UI aperta su una campagna scrape + AI, aggira il pulsante e chiama l'API a mano — il divieto deve reggere anche senza passare dal form:

```bash
curl -s -X PATCH http://localhost:8000/api/campaigns/ID_CAMPAGNA_SCRAPE_CON_AI \
  -H "Content-Type: application/json" \
  -d '{"enrichment_level":"none"}'
```

Expected: `400` col messaggio della guardia. Un `200` qui significa che il gate e' nel posto sbagliato nell'update.

---

### Task 5: Le campagne gia' esistenti + chiusura

**Files:**
- Modify: `docs/architecture/AI_ARCHITECTURE.md`
- Modify: `INDEX.md`, `docs/project/PROGRESS.md`
- Modify: `C:\Users\39342\.claude\projects\d--BOT-OUTBOUND\memory\project_state.md` + `MEMORY.md`

- [ ] **Step 1: Trova le campagne gia' in quello stato**

La guardia non le tocca (vedi «Cosa questo piano NON fa»). Vanno guardate a mano:

```sql
SELECT id, name, status, source_type, ai_enabled, enrichment_level
FROM campaigns
WHERE source_type = 'scrape'
  AND ai_enabled = true
  AND enrichment_level = 'none';
```

Per ognuna: se sta ancora lavorando, alza il livello a `bio` oppure spegni l'AI. Riporta l'elenco a Tommaso, **non decidere al posto suo** su una campagna in corso.

- [ ] **Step 2: Documenta la regola**

In `docs/architecture/AI_ARCHITECTURE.md`, aggiungi una sezione «Quando l'AI non ha la bio» con la tabella delle tre sorgenti di questo piano e il rimando a `valida_ai_senza_bio`. Il punto da scrivere: il vincolo e' «AI + nessuna bio in arrivo», non «AI + livello none» — chi legge solo il nome del livello arriva alla conclusione sbagliata.

- [ ] **Step 3: Riallinea INDEX.md e PROGRESS.md**

Sezione datata in `PROGRESS.md` con: guardia aggiunta, calibratura scelta (A o B), campagne trovate allo Step 1.

- [ ] **Step 4: Memoria di progetto**

Sezione datata in `project_state.md` (cosa modificato, root cause, file toccati, comportamento atteso) + riga in `MEMORY.md`. Obbligatoria, non opzionale (CLAUDE.md della repo).

- [ ] **Step 5: PR**

```bash
git push -u origin BRANCH
gh pr create --base main
```

Nel corpo della PR: la tabella delle tre sorgenti, la calibratura scelta e perche', e la lista delle campagne trovate allo Step 1.
