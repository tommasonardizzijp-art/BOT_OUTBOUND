# Messaggi template (no-AI) come default, AI dietro toggle — Design

Data: 2026-07-11 · Stato: approvato da Tommaso (sessione 11/07)

## Problema

Oggi ogni DM passa dall'AI (Gemini/Groq/Ollama) che personalizza il template sulla bio del follower. Ma:

- le quote free si esauriscono ogni giorno (Gemini 20 req/giorno, Groq 100k token/giorno) e bloccano le sessioni di invio;
- per molte campagne la personalizzazione non serve: basta un messaggio standard, e l'AI può solo peggiorarlo;
- non esiste un modo per mandare il template così com'è.

## Decisioni prese (con Tommaso)

1. **Varianti no-AI = template A/B/C + spintax** `{Ciao|Hey|Salve}` dentro il testo. Combinazioni moltiplicate senza AI, qualità controllata al 100%.
2. **Toggle AI → due campi**: "Contesto AI" (esistente) + "Istruzioni AI" per-campagna che sovrascrivono il prompt di sistema globale del `.env`.
3. **Migrazione**: campagne esistenti → `ai_enabled=true` (zero cambi a sorpresa); nuove campagne → default `false`. Il toggle e i campi messaggi sono **modificabili anche fuori draft**: i messaggi già generati restano, i prossimi seguono la nuova modalità.

## Schema DB (migration 023, additiva)

| Colonna | Tipo | Default | Note |
|---|---|---|---|
| `campaigns.ai_enabled` | Boolean, NOT NULL | `false` (server_default) | La migration setta `true` sulle campagne esistenti |
| `campaigns.message_template_c` | Text, NULL | — | Terzo template opzionale, simmetrico a `message_template_b` |
| `campaigns.ai_system_prompt` | Text, NULL | — | Istruzioni AI per-campagna; NULL/vuoto = prompt globale |

`messages.template_variant` (String(1), già esistente con 'a'/'b') accoglie anche `'c'`.

## Backend

### Nuovo modulo `app/services/template_renderer.py`

- `resolve_spintax(text, rng) -> str`: espande ogni gruppo `{opz1|opz2|...}` scegliendo a caso. Un solo livello (niente annidati — documentato). Un gruppo senza `|` (es. `{nome}`) NON è spintax e resta intatto.
- `fill_name(text, full_name, username) -> str`: riempie `{nome}`/`[nome]`/`{name}`/... (regex `_NAME_PLACEHOLDER_RE` esistente in `ai_personalizer`) con `full_name` o `@username` — stessa semantica di `_fallback_message`.
- `render_template(template, follower) -> str`: spintax → nome → normalizzazione whitespace/newline (riuso della logica in `_validate_message` per CRLF e righe vuote).
- `pick_template(campaign, rng) -> tuple[str, str]`: sceglie a caso con pesi uguali tra i template compilati (A sempre; B/C se non vuoti). Ritorna `(testo, variante)`. **Unifica** i due meccanismi attuali (50/50 random in `generate_messages_batch`, alternanza `generated % 2` nell'altro percorso) — vale per ENTRAMBE le modalità.

### Punto di generazione (`ai_personalizer.py` / task di pre-generazione)

- `campaign.ai_enabled == False` → `text = render_template(...)`, `status = message_generated`, **nessuna chiamata AI**, nessun consumo quota.
- `campaign.ai_enabled == True` → flusso attuale; il system prompt diventa `campaign.ai_system_prompt or settings.ai_system_prompt or DEFAULT_SYSTEM_PROMPT`.
- Gestione residui: i placeholder nome hanno sempre un valore (fallback `@username`). Un placeholder sconosciuto residuo (es. `{azienda}`, senza `|`) fa fallire il rendering di QUEL messaggio (`TemplateRenderError` → follower `failed`) — stessa semantica dell'attuale `_fallback_message`, che rifiuta di mandare placeholder letterali. Uno spintax malformato (graffa mai chiusa) resta testo letterale + warning nel log. Il frontend segnala entrambi alla validazione del form.

### API (`schemas/campaign.py`, `api/campaigns.py`)

- Create/Update: nuovi campi `ai_enabled`, `message_template_c`, `ai_system_prompt`.
- I campi messaggi (`base_message_template`, `message_template_b`, `message_template_c`, `ai_enabled`, `ai_prompt_context`, `ai_system_prompt`) sono aggiornabili in QUALSIASI stato (non solo draft) — a differenza di `bio_engine` che resta draft-only. Validazione: `base_message_template` obbligatorio se `messaging_enabled`.

### Pipeline invariata

Approvazione campione (`require_approval`), invio, retry, recovery: nessuna modifica. Il messaggio no-AI entra in `message_generated` identico a uno generato dall'AI.

## Frontend

### Form nuova campagna (`app/campaigns/new/page.tsx`)

- Sezione messaggi: Template A (obbligatorio se messaging attivo), bottoni "+ Template B" / "+ Template C".
- Hint sotto i textarea: sintassi `{opzione1|opzione2}` e `{nome}`.
- Bottone "Anteprima varianti": genera client-side 3 esempi (stessa logica spintax duplicata in TS, solo per preview).
- Toggle "Personalizza con AI" (default OFF). ON → compaiono "Contesto AI" (campo esistente) e "Istruzioni AI (opzionale)" con placeholder che spiega l'override del prompt globale.

### Pagina campagna (`app/campaigns/[id]/page.tsx`)

- Stesso blocco (template A/B/C, toggle AI, contesto, istruzioni) editabile anche a campagna avviata/in pausa, con salvataggio via update API.

## Test

- Unit renderer: gruppi spintax multipli, gruppo singolo senza `|` intatto, `{nome}` riempito (full_name / fallback @username), template senza spintax invariato, graffa malformata = testo letterale, normalizzazione newline.
- `pick_template`: solo A → sempre A; A+B+C → tutte e tre escono; variante registrata su `messages.template_variant`.
- Generazione no-AI: mock del client AI che ESPLODE se chiamato → il branch no-AI non lo tocca; messaggio arriva `message_generated` col testo renderizzato.
- Override prompt: campagna con `ai_system_prompt` → usato al posto del globale; vuoto → globale.
- API: update dei campi messaggi su campagna `running` accettato; `bio_engine` resta rifiutato fuori draft.
- Migration: default `false` su nuova riga, esistenti a `true`.

## Fuori scope (esplicito)

- Spintax annidato.
- Fallback DM via `direct/new` per profili senza bottone/voce menu (rimandato — decisione 11/07).
- Tabella template separata (3 colonne bastano; si generalizza solo se servissero N>3).
