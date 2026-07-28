# ARCHITETTURA AI — `ai_personalizer.py`

Come nasce il testo di un DM: rendering locale (default) o generazione AI (opt-in per campagna). Estratto da `CLAUDE.md` il 2026-07-29 (contenuto invariato).

> Per la **taratura della qualità** dei messaggi AI (modello, temperature, system/user prompt, validazione) vedi [AI_MESSAGES.md](AI_MESSAGES.md).

---

## Modalità messaggi: rendering locale (default) vs AI (opt-in per-campagna) — Template mode

Il testo del DM **di default NON passa dall'AI**: `compose_message(campaign, follower)` (`ai_personalizer.py`) è l'**unica entry point** usata dai 4 call-site che generano testo (`generate_preview_batch`, `generate_messages_batch`, `campaign_orchestrator._get_or_create_message`, `followers.regenerate_message`) e decide così:

1. `pick_template()` (`template_renderer.py`) sceglie a caso, pesi uguali, tra i template compilati della campagna (A = `base_message_template`, B = `message_template_b`, C = `message_template_c`, D = `message_template_d` — B/C/D opzionali; D aggiunto in migrazione 024).
2. `campaign.ai_enabled` (bool, **default False** sulle nuove campagne) decide il branch:
   - **False** (default) → `render_template()`: risolve SEMPRE lo spintax `{a|b|c}` e il placeholder nome (`{nome}`/`{name}`/`[nome]`/`[name]`), **zero chiamate AI, istantaneo**. Solleva `TemplateRenderError` se restano placeholder sconosciuti (es. `{azienda}`) o il risultato è vuoto dopo il render — meglio un messaggio fallito (retry/skip) che un DM col placeholder letterale o vuoto.
   - **True** (opt-in per-campagna) → passa dal flusso `generate_message()` sotto, usando `campaign.ai_prompt_context` per il contesto e `campaign.ai_system_prompt` come override per-campagna del system prompt (vuoto/null = usa `AI_SYSTEM_PROMPT`/`DEFAULT_SYSTEM_PROMPT` globali).
3. Migrazione **023**: le campagne preesistenti sono state impostate `ai_enabled=True` (comportamento invariato, era l'unico flusso prima di questa feature); le nuove campagne nascono `ai_enabled=False`.

Frontend: form nuova campagna e dialog di modifica campagna (dettaglio campagna) espongono template C, toggle "Personalizza con AI" + campo "Istruzioni AI" condizionale, hint spintax e bottone anteprima varianti (`frontend/lib/spintax.ts`, usato SOLO per l'anteprima UI — il rendering reale che conta è quello Python sopra). Badge 🤖/📋 sulla card "Template messaggio" indica la modalità attiva della campagna.

A livello **API**, questi campi (`base_message_template`, `message_template_b/c`, `ai_prompt_context`, `ai_enabled`, `ai_system_prompt`) sono `always_editable` in `update_campaign` — passano in **qualsiasi stato** della campagna, incluso `running` (letti freschi a ogni generazione: i messaggi già generati restano, i successivi seguono la nuova modalità; vedi `tests/test_template_mode_api.py`). Il dialog di modifica sul frontend espone la stessa possibilità: il bottone "Modifica" nella card è visibile in ogni stato, e `handleSaveTemplate` include `messaging_enabled` nel payload **solo se cambiato** (non è `always_editable`: invariato va omesso, altrimenti un update a campagna `running` prenderebbe 400). Il toggle "Invia messaggi" nel dialog resta disabilitato fuori da `draft/ready/paused/completed` con hint dedicato — template e campi AI invece si salvano anche a campagna in corso.

---

## Provider

Il layer AI sotto (branch `ai_enabled=True`) supporta tre provider configurabili via `.env`:

| Provider | Config | Default model | Note |
|---|---|---|---|
| `ollama` | nessuna API key | `OLLAMA_MODEL` | locale, lento, qualità bassa su modelli piccoli |
| `groq` | `AI_API_KEY=gsk_...` | `llama-3.3-70b-versatile` | gratis, OpenAI-compatible, raccomandato |
| `gemini` | `AI_API_KEY=AIza... o AQ....` | `gemini-2.5-flash` | gratis, REST API propria; thinking disattivato (thinkingBudget=0). Groq free tier 70b = solo 100k token/giorno |

## Parametri chiave
- `AI_TEMPERATURE=0.35` — bassa per messaggi B2B consistenti (non alzare oltre 0.5)
- `AI_SYSTEM_PROMPT` — se vuoto, usa il default ottimizzato in `ai_personalizer.py:DEFAULT_SYSTEM_PROMPT`
- Il system prompt default: ruolo B2B, regole numerate per priorità, "preserva struttura template", "grammaticalmente corretto", "non inventare dalla bio"

## Flusso generazione
1. `generate_message()` → legge `settings.ai_provider` → branch sul provider
2. `_build_user_prompt()` → costruisce il prompt utente con template + bio + contesto campagna
3. `_get_system_prompt()` → usa `AI_SYSTEM_PROMPT` da .env oppure `DEFAULT_SYSTEM_PROMPT`
4. `_validate_message()` → strip virgolette, **preserva gli a-capo `\n`** (normalizza CRLF, collassa 3+ righe vuote), truncate, fallback. ⚠️ Gli a-capo NON sono più collassati: a send-time `_human_type` li batte come `Shift+Enter` (Enter da solo invierebbe il DM su IG web). Vedi flusso `send_dm` in [BROWSER.md](BROWSER.md).

---

Vedi anche: [AI_MESSAGES.md](AI_MESSAGES.md) · [DATABASE.md](DATABASE.md) · [../setup/CONFIGURAZIONE.md](../setup/CONFIGURAZIONE.md)
