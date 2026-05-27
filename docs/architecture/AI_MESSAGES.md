# AI_MESSAGES — Qualità dei messaggi generati

Tutti i parametri controllabili per migliorare i DM generati da Ollama.  
File di riferimento: `backend/app/services/ai_personalizer.py`

---

## Stato attuale

| Parametro | Valore attuale | Qualità |
|---|---|---|
| Modello | `llama3.2` (3B) | ❌ Troppo piccolo per italiano con contesto |
| Temperature | 0.85 | ⚠️ Troppo alto → creatività > coerenza |
| top_p | 0.9 | ⚠️ Slightly high |
| num_predict | 200 | ⚠️ Troppo largo → messaggi lunghi/divaganti |
| top_k | non impostato | ⚠️ Mancante |
| repeat_penalty | non impostato | ⚠️ Mancante → ripetizioni frequenti |
| Few-shot examples | assenti | ❌ Il modello non sa il formato atteso |
| Lingua esplicita | non specificata | ⚠️ Il modello può rispondere in inglese |

**Diagnosi principale**: `llama3.2` (3B) non segue bene istruzioni complesse in italiano.  
Il second problema è che il system prompt non ha esempi concreti.

---

## Layer 1 — Modello [`OLLAMA_MODEL` in `.env`]

**Cambio immediato, zero codice.**

| Modello | Italiano | Velocità | RAM | Consigliato |
|---|---|---|---|---|
| `llama3.2` | ⭐⭐ | ⚡⚡⚡⚡ | 3GB | Attuale — troppo piccolo |
| `llama3.1:8b` | ⭐⭐⭐⭐ | ⚡⚡⚡ | 8GB | **Miglior rapporto qualità/velocità** |
| `mistral:7b` | ⭐⭐⭐⭐ | ⚡⚡⚡ | 7GB | Buono per istruzioni precise |
| `gemma2:9b` | ⭐⭐⭐⭐⭐ | ⚡⚡ | 9GB | Migliore qualità testo corto |
| `phi3:14b` | ⭐⭐⭐⭐⭐ | ⚡ | 14GB | Solo se hai GPU/RAM abbondante |

**Raccomandazione**: inizia con `llama3.1:8b`.

```bash
ollama pull llama3.1:8b
# poi in .env:
OLLAMA_MODEL=llama3.1:8b
```

**Stato**: ☐ Da fare

---

## Layer 2 — Parametri generazione Ollama

**File**: `backend/app/services/ai_personalizer.py` — riga 62

### Valori attuali vs raccomandati

```python
# ATTUALE
"temperature": 0.85,
"top_p": 0.9,
"num_predict": 200,

# RACCOMANDATO
"temperature": 0.65,      # meno allucinazioni, più coerente
"top_p": 0.85,            # vocabolario leggermente più controllato
"num_predict": 120,       # forza brevità (DM deve essere corto)
"top_k": 40,              # aggiungere — limita vocabolario ai 40 token più probabili
"repeat_penalty": 1.15,   # aggiungere — penalizza ripetizioni di parole
```

### Guida ai parametri

| Parametro | Range utile | Effetto sull'output |
|---|---|---|
| `temperature` | 0.5–0.9 | Alto = creativo ma incoerente. Basso = noioso ma preciso. Per DM: 0.6-0.7 |
| `top_p` | 0.7–0.95 | Nucleus sampling. Scende → meno sorprese. Alzare se output troppo piatto. |
| `top_k` | 20–60 | Limita scelta a K token. 40 è buon default. |
| `num_predict` | 80–200 | Max token. 120 ≈ 3-4 frasi in italiano. Non di più per un DM. |
| `repeat_penalty` | 1.0–1.3 | 1.0 = nessuna penalità. 1.15 elimina ripetizioni fastidiose. Non sopra 1.3. |
| `seed` | qualunque int | Riproducibilità. Non usare in produzione (messaggi identici). |

**Stato**: ☐ Da fare

---

## Layer 3 — System Prompt

**File**: `backend/app/services/ai_personalizer.py` — riga 17

### Problemi del system prompt attuale

1. Nessun esempio concreto (few-shot) → il modello inventa il formato
2. Regole solo in negativo ("NON usare...") → meno efficaci
3. Non specifica la lingua → modello può rispondere in inglese
4. "2-4 frasi" è vago → non controlla la struttura

### System prompt migliorato (proposta)

```python
SYSTEM_PROMPT = """Sei un copywriter esperto di DM Instagram per brand italiani.
Scrivi messaggi brevi, personali e naturali — come scriverebbe una persona reale, non un bot.

REGOLE FONDAMENTALI:
- Scrivi SOLO in italiano
- Massimo 3 frasi. Non di più.
- Usa il nome della persona se disponibile
- Se la bio contiene qualcosa di specifico (professione, passione, luogo), menzionalo in modo naturale
- Non iniziare mai con "Ciao" o "Hey"
- Non usare punti esclamativi multipli
- Non sembrare un copy pubblicitario
- Rispondi SOLO con il testo del messaggio, senza prefissi o spiegazioni

ESEMPI:

Bio: "Chef romano, amo i sapori autentici 🍝 | Milano"
Template: "Ho visto che ti occupi di food, ho qualcosa che potrebbe interessarti"
Output: Ho notato che sei uno chef con radici romane — lavoro con diversi professionisti del settore food e ho qualcosa di specifico per chi come te punta all'autenticità. Ti scrivo?

Bio: "Personal trainer | Aiuto le persone a trasformarsi 💪 Torino"
Template: "Sto cercando professionisti del fitness per una collaborazione"
Output: Seguo il tuo lavoro da un po' — quello che fai con i tuoi clienti mi sembra esattamente l'approccio giusto. Ho una proposta concreta per trainer come te, posso dirtela in 2 righe?

Bio: "" (vuota)
Template: "Ciao, volevo presentarmi"
Output: Ho visto il tuo profilo e volevo farti una proposta veloce — ci vorranno letteralmente 30 secondi. Ha senso?"""
```

**Modifiche chiave rispetto all'attuale**:
- Lingua esplicita (`SOLO in italiano`)
- 3 frasi massimo invece di "2-4"
- 3 esempi few-shot con input/output reali
- Regola positiva "menzionalo in modo naturale" invece di solo negazioni
- Esempio con bio vuota → il modello sa cosa fare anche in quel caso

**Stato**: ☐ Da fare

---

## Layer 4 — User Prompt (contesto per singolo messaggio)

**File**: `backend/app/services/ai_personalizer.py` — riga 48

### Struttura attuale

```python
user_prompt = f"""Template base del messaggio:
{base_template}

{f"Contesto aggiuntivo: {ai_context}" if ai_context else ""}

Destinatario:
- Username: @{follower_username}
- Nome: {name}
- Bio Instagram: {f'"{bio_text}"' if bio_text else "(bio vuota)"}

Scrivi il messaggio DM personalizzato:"""
```

### Miglioramenti proposti

```python
user_prompt = f"""TEMPLATE (mantieni l'intento, varia le parole):
{base_template}

{f"CONTESTO BRAND/OFFERTA:{chr(10)}{ai_context}{chr(10)}" if ai_context else ""}
DESTINATARIO:
- Nome: {name}
- Bio: {f'"{bio_text}"' if bio_text else "nessuna bio disponibile — scrivi un messaggio generico ma non banale"}

Scrivi il messaggio (massimo 3 frasi, SOLO in italiano):"""
```

**Differenze**:
- `"mantieni l'intento, varia le parole"` → riduce i casi in cui il modello copia il template verbatim
- Istruzione esplicita quando bio è vuota invece di silenzio
- Ripete "massimo 3 frasi" e "SOLO in italiano" nel user prompt (rinforzo)
- `CONTESTO BRAND` più prominente nel testo

**Stato**: ☐ Da fare

---

## Layer 5 — Campo `ai_prompt_context` della campagna

**Nessun codice richiesto — solo compilare bene il form.**

Questo campo è la leva più rapida. Viene inserito direttamente nel prompt come contesto brand.

### Esempio di `ai_prompt_context` scarso (attuale tipico)
```
Voglio vendere il mio corso di marketing
```

### Esempio ottimizzato
```
Brand: [nome]
Offerta: corso online di marketing digitale per imprenditori, 6 moduli, accesso lifetime
Target ideale: imprenditori, freelance, creator con audience propria
Tone of voice: diretto, senza fronzoli, rispettoso del tempo altrui
Obiettivo del DM: aprire una conversazione, non vendere direttamente
Cosa NON dire: prezzi, sconti, "opportunità unica"
```

**Stato**: ☐ Documentare nelle istruzioni per l'utente (`docs/guides/GUIDA.md`)

---

## Layer 6 — Validazione output

**File**: `backend/app/services/ai_personalizer.py` — riga 92

### Problema attuale

```python
if len(message) > 500:
    message = message[:497] + "..."  # ← tronca → messaggio spezzato
```

### Fix proposto

```python
if len(message) > 500:
    logger.warning("Generated message too long, regenerating...")
    return _fallback_message(base_template, fallback_name)
    # oppure: raise OllamaError("message too long") → trigger @async_retry
```

Meglio rifiutare e rigenerare che inviare un messaggio troncato con "...".

### Aggiungere validazione qualità (opzionale)

```python
# Segnali di output scarso:
BAD_PATTERNS = [
    "come assistente AI",
    "non posso",
    "mi dispiace",
    "ecco il messaggio",
    "Messaggio:",
    "Output:",
    "---",
]
if any(p.lower() in message.lower() for p in BAD_PATTERNS):
    return _fallback_message(base_template, fallback_name)
```

**Stato**: ☐ Da fare (fix troncamento — 2 righe)

---

## Priorità di intervento

| # | Intervento | Impatto | Effort | Stato |
|---|---|---|---|---|
| 1 | Cambiare modello → `llama3.1:8b` | ★★★★★ | 0 codice, solo `.env` + `ollama pull` | ☐ |
| 2 | Few-shot nel system prompt (Layer 3) | ★★★★★ | 30 min — solo modifica stringa | ☐ |
| 3 | Compilare bene `ai_prompt_context` per ogni campagna | ★★★★ | 0 codice | ☐ |
| 4 | Parametri Ollama (Layer 2) | ★★★★ | 5 min | ☐ |
| 5 | User prompt migliorato (Layer 4) | ★★★ | 10 min | ☐ |
| 6 | Fix troncamento → rigenerazione (Layer 6) | ★★★ | 5 min | ☐ |
| 7 | Validazione bad patterns (Layer 6) | ★★ | 15 min | ☐ |
