# Configurazione Telegram Bot â€” BOT OUTBOUND

## 1. Crea il bot con BotFather

1. Apri Telegram e cerca **@BotFather** (verificato con spunta blu)
2. Invia `/newbot`
3. Scegli un **nome** (es. `BOT OUTBOUND Monitor`)
4. Scegli un **username** (deve finire in `bot`, es. `bot_outbound_monitor_bot`)
5. BotFather risponde con:
   ```
   Done! Use this token to access the HTTP API:
   1234567890:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
6. Copia il token â€” Ã¨ il tuo `TELEGRAM_BOT_TOKEN`

## 2. Ottieni il tuo Chat ID

**Metodo A (consigliato â€” usa @userinfobot):**
1. Cerca @userinfobot su Telegram
2. Invia `/start`
3. Risponde con il tuo ID numerico (es. `123456789`)
4. Questo Ã¨ il tuo `TELEGRAM_CHAT_ID`

**Metodo B (via API):**
1. Manda un messaggio qualsiasi al tuo bot (cerca il suo username)
2. Apri nel browser: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Cerca `"chat":{"id":` nel JSON â€” quello Ã¨ il chat_id

> **Nota:** Se vuoi ricevere notifiche su un **gruppo**, aggiungi il bot al gruppo
> e usa il chat_id negativo del gruppo (es. `-1001234567890`).

## 3. Configura .env

Apri `d:\BOT OUTBOUND\.env` e inserisci:

```env
TELEGRAM_BOT_TOKEN=1234567890:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

## 4. Abilita i comandi Telegram (opzionale ma consigliato)

Invia a BotFather:
```
/setcommands
```
Seleziona il tuo bot, poi incolla:
```
status - Stato bot e campagne attive
pause - Scegli una campagna attiva da mettere in pausa
resume - Scegli una campagna in pausa da riprendere
halt - Kill-switch globale di emergenza (usa: /halt motivo)
unhalt - Sblocca il kill-switch globale
logs - Ultimi 8 log di attivitÃ 
anomalies - Anomalie ultime 24h
```

## 5. Test notifiche

Riavvia il backend, poi testa:
```bash
cd backend
./venv/Scripts/python.exe -c "
import asyncio
from app.services.notifier import send_telegram
asyncio.run(send_telegram('Test notifica BOT OUTBOUND âœ…', level='info'))
"
```
Dovresti ricevere il messaggio su Telegram entro pochi secondi.

## 6. Comandi disponibili da Telegram

Una volta avviato il bot e il worker ARQ, puoi controllare il bot da Telegram:

| Comando | Azione |
|---------|--------|
| `/status` | Stato bot (RUNNING/HALTED), campagne attive, account attivi |
| `/pause` | Mostra bottoni inline con le campagne attive; mette in pausa solo quella selezionata |
| `/resume` | Mostra bottoni inline con le campagne in pausa; riprende solo quella selezionata |
| `/halt motivo facoltativo` | Attiva il kill-switch globale di emergenza |
| `/unhalt` | Sblocca il kill-switch globale e riaccoda il lavoro ancora attivo |
| `/logs` | Ultimi 8 log di attivitÃ  |
| `/anomalies` | Anomalie delle ultime 24h |

> I comandi vengono processati dal cron ARQ ogni 30 secondi circa (configurabile).
> Solo messaggi provenienti dal `TELEGRAM_CHAT_ID` configurato vengono accettati.

## 7. Notifiche automatiche ricevute

Il bot invia automaticamente:

- ðŸ“Š **Mini-session recap** â€” a fine ogni sessione DM (5-20 mess): account, inviati/falliti/saltati, lista profili, pausa e orario ripresa
- âš ï¸ **Warning** â€” dm_failed_streak, dm_recovery_no_evidence, worker_crash
- ðŸš¨ **Errori** â€” consecutive_unexpected_errors, challenge richiesta
- ðŸ”¥ **Critical + kill-switch** - soglie sistemiche come troppi ban o challenge ripetuti
- ðŸ“¸ **Screenshot** allegato automatico â€” su ban account e challenge richiesta

## 8. Variabili .env opzionali (valori di default consigliati)

```env
# Telegram
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>

# Comandi Telegram (polling ARQ)
# Lasciare a true per abilitare /pause /resume /halt /unhalt /status dal telefono
TELEGRAM_COMMANDS_ENABLED=true
TELEGRAM_POLL_TIMEOUT_SECONDS=5

# Recap mini-sessione
TELEGRAM_SESSION_RECAP_ENABLED=true

# Anomaly auto-stop
ANOMALY_AUTO_STOP_ENABLED=true
ANOMALY_BAN_THRESHOLD_PER_HOUR=3         # 3 ban/h -> halt globale
ANOMALY_CONSECUTIVE_DM_FAILURES=5        # 5 fail streak â†’ pausa campagna
ANOMALY_CHALLENGE_THRESHOLD_PER_DAY=3    # 3 challenge/24h â†’ halt globale
ANOMALY_WORKER_CRASH_THRESHOLD_PER_HOUR=3
```

