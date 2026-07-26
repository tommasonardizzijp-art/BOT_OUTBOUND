# backend/scripts/poc_wa/poc2_send.py
"""PoC-2b — guardia pre-invio + invio reale + verifica spunte.

Sequenza per ogni destinatario (identica a quella che il sender di M3 dovra' fare):
  0. numero gia' in opt-out (D:\\wa-poc\\optout.json) -> NON si apre nemmeno la chat;
  1. apre la chat con la strategia vincente di PoC-2a;
  2. GUARDIA PRE-INVIO: legge i messaggi inbound successivi all'ultimo messaggio
     nostro e cerca uno STOP -> cronometrata, target <= 2s (SDD 13, PoC-2);
  3. se STOP -> NON invia, scrive l'opt-out (vale per sempre) e passa oltre;
  4. se la coda inbound non e' stata agganciata (sentinella, vedi JS_TAIL) -> NON
     invia: non sappiamo se c'e' uno STOP nascosto;
  5. altrimenti digita in modo umano un testo non ancora mandato a questo
     destinatario (SentLog) e invia;
  6. legge la spunta dell'ultimo messaggio inviato (orologio/1/2) -> Q39.

QUATTRO GUARDIE prima di scrivere a qualcuno:
  - allowlist fail-closed (POC_WA_ALLOWED_NUMBERS);
  - flag --send esplicito (senza, e' dry-run: apre, misura la guardia, non invia);
  - STOP trovato = stop assoluto (persistito: uno STOP visto una volta vale per
    sempre, anche quando il DOM non lo mostra piu');
  - coda inbound non agganciata = stop per prudenza (non e' silenzio, e' cecita').

Uso:  python poc2_send.py --numero "+39..." --messaggio-file D:\\wa-poc\\messages.txt [--send]
"""
import argparse
import asyncio
import csv
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from _common import artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from poc2_open import COMPOSER_SEL, open_by_deeplink
from poc_state import OptOutStore, SentLog
from wa_lib import AllowList, contains_stop, load_messages, mask_pii, normalize_e164

# Legge la coda di messaggi in fondo alla conversazione: gli inbound dopo
# l'ultimo outbound nostro. Budget fisso: cio' che e' renderizzato, niente scroll
# infinito (SDD: budget = visibili + 1-2 scroll).
# ATTENZIONE — selettori NON ancora verificati sul DOM reale (lo fa il Task 5,
# step 1b). Se 'div.message-in'/'div.message-out' non agganciano nulla, la
# funzione ritorna null (nessuna bolla trovata) e la guardia ABORTISCE
# l'invio (A2): senza questa sentinella concluderebbe "nessuno STOP" e
# invierebbe SEMPRE, sembrando funzionare. Prima di usare questo script con
# --send, controlla nel catalogo (docs/whatsapp/wa-dom-catalog.md) che i
# conteggi delle bolle siano > 0.
JS_TAIL = """
() => {
  const rows = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
  if (rows.length === 0) return null;   // nessuna bolla agganciata: sentinella, non "coda vuota"
  const tail = [];
  for (let i = rows.length - 1; i >= 0 && tail.length < 30; i--) {
    const el = rows[i];
    const isOut = el.classList.contains('message-out');
    if (isOut) break;                     // fermati al nostro ultimo messaggio
    tail.push((el.innerText || '').slice(0, 300));
  }
  return tail.reverse();
}
"""
TICK_SEL = ["[data-icon='status-dblcheck']", "[data-icon='status-check']", "[data-icon='status-time']"]

# Colonne del CSV di audit (A6): una riga "invio" viene scritta SUBITO dopo
# l'Enter (prima di leggere la spunta, che puo' impiegare secondi ed eccepire),
# poi se la lettura della spunta riesce si scrive una seconda riga di
# aggiornamento con lo stesso invio_id. Append-only di proposito: mai riscrivere
# una riga gia' scritta, e' un log di audit su invii reali.
_CSV_HEADER = [
    "ts", "numero_masked", "invio_id", "evento",
    "open_ms", "guardia_dom_ms", "guardia_totale_ms",
    "inbound_letti", "coda_non_agganciata", "stop_trovato",
    "inviato", "spunta", "totale_ms", "note",
]


def _scrivi_riga_csv(path: Path, **campi) -> None:
    """Append di una riga; scrive l'header alla primissima riga (file assente)."""
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(_CSV_HEADER)
        w.writerow([campi.get(c, "") for c in _CSV_HEADER])


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def guardia_pre_invio(page) -> tuple[list[str] | None, float, bool]:
    """Ritorna (inbound_dopo_ultimo_nostro, millisecondi, stop_trovato).

    `inbound_dopo_ultimo_nostro` e' None quando JS_TAIL non ha agganciato
    NESSUNA bolla nel DOM (sentinella A2): distinto da lista vuota, che
    significa "nessun inbound dopo il nostro ultimo messaggio" (caso normale).
    """
    t0 = time.perf_counter()
    tail = await page.evaluate(JS_TAIL)
    ms = (time.perf_counter() - t0) * 1000
    stop = any(contains_stop(t) for t in tail) if tail is not None else False
    return tail, ms, stop


async def leggi_spunta(page) -> str:
    found = await first_locator(page, TICK_SEL, timeout_ms=8000)
    return found[1] if found else "nessuna-spunta-letta"


async def main(numero: str, testi: list[str], send: bool) -> None:
    allow = AllowList.load()
    if not len(allow):
        raise SystemExit("POC_WA_ALLOWED_NUMBERS non configurata: mi fermo (fail-closed).")
    e164 = normalize_e164(numero)
    if not e164:
        raise SystemExit(f"Numero non normalizzabile: {numero!r}")
    allow.assert_allowed(e164)   # fail-closed: solleva se non e' una chat controllata

    optout = OptOutStore.load()
    sentlog = SentLog.load()
    masked = f"…{e164[-4:]}"
    path = artifacts_dir() / "send_results.csv"

    # Guardia 0 (A1 punto 1): numero gia' in opt-out -> non si apre nemmeno la
    # chat di chi ha chiesto di smettere.
    if optout.is_opted_out(e164):
        _scrivi_riga_csv(
            path, ts=_ts(), numero_masked=masked, evento="skip-optout-preesistente",
            inviato=0, note="numero gia' in opt-out: nessuna chat aperta, nessun invio",
        )
        log_event("send_skip_optout", numero_masked=masked)
        return

    async with wa_context(headless=False) as (context, page):
        t_start = time.perf_counter()
        ok, open_ms, segnale = await open_by_deeplink(page, e164)
        # Gate su `ok`, non sulla sola stringa del segnale (A3): un segnale che
        # non contiene 'nessuna-cronologia' NON significa "ha cronologia, procedi"
        # se l'apertura stessa (ok) e' fallita.
        if not ok:
            await snap(page, "poc2-send-apertura-fallita")
            raise SystemExit(f"Chat non aperta ({segnale}): niente invio.")
        if "nessuna-cronologia" in segnale:
            raise SystemExit("Chat senza cronologia: V2 vieta di scrivere. Stop.")

        tail, guardia_dom_ms, stop = await guardia_pre_invio(page)
        coda_non_agganciata = tail is None
        inbound_letti = len(tail) if tail is not None else 0
        guardia_totale_ms = open_ms + guardia_dom_ms  # costo vero del controllo opt-out (A6)

        inviato, spunta, note, testo_scelto = False, "", "", None
        if coda_non_agganciata:
            # Sentinella A2: non sappiamo se c'e' uno STOP nascosto, non si invia.
            note = "coda inbound non agganciata (JS_TAIL=null): invio abortito per prudenza"
        elif stop:
            optout.add(e164, motivo="STOP rilevato nella coda inbound pre-invio")
            note = "STOP trovato nella coda inbound: invio annullato, opt-out registrato"
        elif not send:
            note = "dry-run (nessun --send)"
        else:
            candidati = [t for t in testi if not sentlog.already_sent(e164, t)]
            if not candidati:
                note = "tutti i testi disponibili gia' mandati a questo destinatario: invio abortito"
            else:
                testo_scelto = random.choice(candidati)
                comp = await first_locator(page, COMPOSER_SEL, timeout_ms=10000)
                if not comp:
                    await snap(page, "poc2-composer-non-trovato")
                    raise SystemExit("Composer non trovato: catalogare il selettore prima di riprovare.")
                await human_type(page, comp[0], testo_scelto)
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await page.keyboard.press("Enter")
                inviato = True

                # A6: la riga di audit va scritta SUBITO dopo l'Enter (irreversibile),
                # non in fondo al percorso felice. Se leggi_spunta eccepisce dopo
                # questo punto, la traccia dell'invio esiste comunque.
                invio_id = _ts()
                totale_parziale_ms = (time.perf_counter() - t_start) * 1000
                _scrivi_riga_csv(
                    path, ts=invio_id, numero_masked=masked, invio_id=invio_id, evento="invio",
                    open_ms=round(open_ms), guardia_dom_ms=round(guardia_dom_ms),
                    guardia_totale_ms=round(guardia_totale_ms), inbound_letti=inbound_letti,
                    coda_non_agganciata=int(coda_non_agganciata), stop_trovato=int(stop),
                    inviato=1, spunta="", totale_ms=round(totale_parziale_ms),
                    note="inviato, in attesa lettura spunta",
                )
                sentlog.record(e164, testo_scelto)

                await page.wait_for_timeout(2500)
                spunta = await leggi_spunta(page)
                note = "invio completato, spunta letta"
                totale_ms = (time.perf_counter() - t_start) * 1000
                _scrivi_riga_csv(
                    path, ts=_ts(), numero_masked=masked, invio_id=invio_id, evento="spunta",
                    open_ms=round(open_ms), guardia_dom_ms=round(guardia_dom_ms),
                    guardia_totale_ms=round(guardia_totale_ms), inbound_letti=inbound_letti,
                    coda_non_agganciata=int(coda_non_agganciata), stop_trovato=int(stop),
                    inviato=1, spunta=spunta, totale_ms=round(totale_ms), note=note,
                )
                log_event("send_attempt", guardia_totale_ms=round(guardia_totale_ms),
                           inbound=inbound_letti, stop=stop, inviato=inviato, spunta=spunta,
                           totale_ms=round(totale_ms),
                           ultimo_inbound=mask_pii(tail[-1] if tail else "", keep=60))
                return

        # Rami che NON hanno inviato (coda non agganciata / stop / dry-run / niente testo nuovo):
        # una riga sola basta, non c'e' nulla di irreversibile da tracciare in due tempi.
        totale_ms = (time.perf_counter() - t_start) * 1000
        _scrivi_riga_csv(
            path, ts=_ts(), numero_masked=masked, evento="esito", open_ms=round(open_ms),
            guardia_dom_ms=round(guardia_dom_ms), guardia_totale_ms=round(guardia_totale_ms),
            inbound_letti=inbound_letti, coda_non_agganciata=int(coda_non_agganciata),
            stop_trovato=int(stop), inviato=int(inviato), spunta=spunta,
            totale_ms=round(totale_ms), note=note,
        )
        log_event("send_attempt", guardia_totale_ms=round(guardia_totale_ms), inbound=inbound_letti,
                   stop=stop, inviato=inviato, spunta=spunta, totale_ms=round(totale_ms), note=note,
                   ultimo_inbound=mask_pii(tail[-1] if tail else "", keep=60))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", required=True)
    ap.add_argument("--messaggio-file", required=True, help="file con i messaggi veri scritti da Tommaso")
    ap.add_argument("--send", action="store_true", help="senza questo flag e' dry-run")
    args = ap.parse_args()
    testi = load_messages(args.messaggio_file)   # solleva MessagesFileError se vuoto/malformato (A7)
    asyncio.run(main(args.numero, testi, args.send))
