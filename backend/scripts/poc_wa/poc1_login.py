# backend/scripts/poc_wa/poc1_login.py
"""PoC-1a — login iniziale via QR + marker di inizio sessione.

Da eseguire UNA VOLTA, con il telefono in mano. Da qui parte il cronometro dei
14 giorni: ogni re-scan richiesto dopo questo momento e' un dato di PoC-1.
E' idempotente: se la sessione esiste gia' (login fatto a mano, o rilancio) si
limita a riconoscerla e a piantare il marker.

Timeout: il primo avvio di un profilo FREDDO richiede molto piu' di 8 secondi —
WhatsApp Web scarica e costruisce l'app da zero. L'esecuzione del 27/07 e' morta
con "schermata ignota" mentre la pagina era perfettamente loggata: aveva
guardato troppo presto. I timeout qui sotto sono tarati su quell'incidente.

Uso:  python poc1_login.py
"""
import asyncio
from datetime import datetime, timezone

from _common import artifacts_dir, first_locator, log_event, snap, wa_context

# Candidati per "sono loggato": la lista chat e' visibile.
# VERIFICATI sul DOM reale il 27/07 (probe_selettori.py): i primi tre agganciano.
# L'aria-label vero e' "Lista delle chat", NON "Elenco chat" come si era ipotizzato:
# il candidato sbagliato resta solo come promemoria di quanto vale indovinare.
CHATLIST_CANDIDATES = [
    "#pane-side",
    "[data-testid='chat-list']",
    "[role='grid']",
    "div[aria-label*='Lista delle chat']",
    "div[aria-label*='Chat list']",
]
# Candidati per "serve il QR". NON VERIFICATI: al primo contatto (27/07) la
# sessione era gia' stata stabilita a mano, quindi nessuna schermata QR e' mai
# stata osservata. Se PoC-1 provoca un logout, il primo rilancio la cataloga:
# per questo, quando nessun candidato aggancia, lo script fa lo screenshot
# invece di dichiarare un fallimento e basta.
QR_CANDIDATES = [
    "canvas[aria-label*='Scan']",
    "canvas[aria-label*='scansiona' i]",
    "[data-testid='qrcode']",
    "[data-ref]",
    "canvas",
]

# Il primo avvio di un profilo freddo puo' superare il minuto (download +
# costruzione dell'app + sync iniziale delle chat). Meglio aspettare a vuoto
# che dichiarare un falso NO-GO su un PoC che dura 14 giorni.
TIMEOUT_CHATLIST_MS = 90000
TIMEOUT_QR_MS = 30000


async def main() -> None:
    async with wa_context(headless=False) as (context, page):
        print(f"Attendo la lista chat (fino a {TIMEOUT_CHATLIST_MS // 1000}s: profilo freddo = app da costruire)…")
        found = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=TIMEOUT_CHATLIST_MS)
        if found:
            _, sel = found
            log_event("already_logged_in", selector=sel)
            print(f"Sessione gia' attiva (agganciata con {sel}): nessun QR necessario.")
        else:
            qr = await first_locator(page, QR_CANDIDATES, timeout_ms=TIMEOUT_QR_MS)
            if not qr:
                await snap(page, "poc1-schermata-ignota")
                log_event("login_unknown_screen")
                raise SystemExit(
                    "Ne' lista chat ne' QR: schermata non prevista. "
                    "Guarda lo screenshot in artifacts/ e catalogala. "
                    "Attenzione (lezione 27/07): prima di concludere che i selettori "
                    "sono sbagliati, verifica sullo screenshot che la pagina non fosse "
                    "semplicemente ancora in caricamento."
                )
            _, qr_sel = qr
            log_event("qr_shown", selector=qr_sel)
            print("Inquadra il QR col telefono (Dispositivi collegati). Attendo fino a 3 minuti…")
            got = await first_locator(page, CHATLIST_CANDIDATES, timeout_ms=180000)
            if not got:
                await snap(page, "poc1-login-fallito")
                raise SystemExit("Login non completato entro 3 minuti.")
            _, sel = got
            log_event("login_ok", selector=sel)

        # Quante chat ha davvero questo numero: aria-rowcount e' il totale, le
        # righe nel DOM sono solo quelle renderizzate. Il 27/07 il divario era
        # 67 su 485 -> la lista e' virtualizzata sul serio e PoC-3 dovra' tenerne
        # conto (Q40). Si misura qui perche' e' gratis e serve subito.
        try:
            griglia = page.locator("[role='grid']").first
            totale = await griglia.get_attribute("aria-rowcount")
            rese = await page.locator("[role='row']").count()
            log_event("chat_count", totale_dichiarato=totale, righe_nel_dom=rese)
            print(f"Chat totali dichiarate: {totale} · righe renderizzate ora: {rese}")
        except Exception as exc:
            log_event("chat_count_fallito", errore=f"{type(exc).__name__}: {exc}"[:200])

        # Marker: da qui si contano i giorni di sessione viva.
        marker = artifacts_dir() / "session_start.txt"
        if not marker.exists():
            marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
            print(f"Marker piantato: {marker} -> t0 dei 14 giorni di PoC-1.")
        else:
            print(f"Marker gia' presente ({marker.read_text(encoding='utf-8').strip()}): NON sovrascritto.")
        await snap(page, "poc1-logged-in")
        log_event("session_established", marker=str(marker))
        print("OK. Da ora gira poc1_heartbeat.py ogni giorno (il primo di giornata con --nota 'dopo riavvio PC').")


if __name__ == "__main__":
    asyncio.run(main())
