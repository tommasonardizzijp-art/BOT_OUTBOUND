# backend/scripts/poc_wa/poc2_send.py
"""PoC-2b — guardia pre-invio + invio reale + verifica spunte.

Sequenza per ogni destinatario (identica a quella che il sender di M3 dovra' fare):
  0. numero gia' in opt-out (D:\\dev\\wa-poc\\optout.json) -> NON si apre nemmeno la chat;
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

Uso:  python poc2_send.py --numero "+39..." [--messaggio-file D:\\dev\\wa-poc\\messages.txt] [--send]
      (senza --messaggio-file usa POC_WA_MESSAGES / D:\\dev\\wa-poc\\messages.txt)
"""
import argparse
import asyncio
import csv
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from _common import (MESSAGES_FILE, artifacts_dir, carica_cronologia, first_locator,
                     human_type, log_event, snap, wa_context)
from poc2_open import COMPOSER_SEL, open_by_search
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
# RISCRITTO il 27/07 sul DOM reale (probe_conversazione + probe_direzione).
#
# La versione precedente cercava `div.message-in` / `div.message-out`: quelle
# classi NON ESISTONO PIU'. Misurato su una chat vera con 35 messaggi: 0 nodi
# agganciati, su tutte e quattro le varianti provate. La guardia sarebbe
# ritornata `null` a ogni invio -> la sentinella avrebbe bloccato tutto, quindi
# fail-safe, ma inutilizzabile.
#
# Cosa esiste davvero: ogni messaggio e' `#main [data-testid='msg-container']`,
# che porta anche `data-id` (35/35). La DIREZIONE non e' piu' una classe, e le
# classi rimaste sono offuscate (`xa0aww2`, `xscbp6u`) e NON discriminano —
# verificato incrociando i campioni, la prima analisi le aveva indicate per un
# artefatto di campione piccolo.
#
# Tre segnali di direzione, misurati:
#   1. <span aria-label="Tu:"> dentro il messaggio  -> nostro. Semantico e
#      chiaro, ma ITALIANO: se l'interfaccia cambia lingua smette di funzionare.
#      Assente su ~20% dei messaggi (blocchi consecutivi).
#   2. data-icon 'tail-out' / 'tail-in' -> la coda grafica della bolla. Sicuro
#      ma presente solo sul primo messaggio di ogni blocco (15/35).
#   3. `data-id`: 20 caratteri con prefisso "3A" -> IN, 32 caratteri con
#      prefisso "A5" -> OUT. Copertura 100%, coerente 12/12 con i tail.
#      NON documentato da WhatsApp: usato come segnale, mai da solo.
#
# REGOLA DI COMBINAZIONE, deliberatamente asimmetrica: un messaggio e' "nostro"
# solo se ALMENO UN segnale dice OUT e NESSUNO dice IN. In ogni altro caso —
# discordanza, o nessun segnale — vale INBOUND, quindi lo si legge.
# Il motivo e' che i due errori non costano uguale:
#   - trattare un nostro messaggio come inbound = si legge qualche messaggio in
#     piu' del necessario; al peggio si trova uno STOP vecchio e NON si invia.
#   - trattare un inbound come nostro = ci si ferma prima di leggerlo, e uno
#     STOP appena arrivato non viene visto. Si scrive a chi ha chiesto di
#     smettere. Inaccettabile.
# In dubbio, quindi, si legge.
JS_TAIL = """
() => {
  // Un solo nodo per messaggio. `msg-container` e `[data-id]` sono due nodi
  // ANNIDATI, non lo stesso: cercarli insieme raddoppiava la lista (misurato
  // il 27/07 — ogni messaggio compariva due volte, e il duplicato senza
  // data-id aveva un segnale di direzione in meno). Il nodo che porta il
  // data-id porta anche data-testid="conv-msg-<id>": i due selettori qui
  // sotto individuano quindi lo STESSO elemento, e querySelectorAll lo
  // restituisce una volta sola.
  const rows = Array.from(document.querySelectorAll(
    "#main [data-id], #main [data-testid^='conv-msg-']"));
  if (rows.length === 0) return null;   // sentinella: cecita', non "coda vuota"

  const direzione = (el) => {
    let out = false, inn = false;

    // 1. etichetta semantica del mittente (it)
    if (el.querySelector("span[aria-label='Tu:']")) out = true;
    const lab = el.querySelector("span[aria-label$=':']");
    if (lab && lab.getAttribute('aria-label') !== 'Tu:') inn = true;

    // 2. coda grafica della bolla
    if (el.querySelector("[data-icon='tail-out']")) out = true;
    if (el.querySelector("[data-icon='tail-in']")) inn = true;

    // 3. forma del data-id
    const id = el.getAttribute('data-id') || '';
    if (/^3A/.test(id) && id.length <= 24) inn = true;
    else if (/^A5/.test(id) || id.length >= 30) out = true;

    // asimmetrica di proposito: OUT solo se nessun segnale dice IN
    return (out && !inn) ? 'out' : 'in';
  };

  // NON ci si ferma piu' al primo messaggio nostro (cambiato il 27/07 su
  // evidenza di una chat vera). Prima la regola era "leggi cio' che e' arrivato
  // dopo di noi": bastava un messaggio nostro — anche scritto A MANO dal
  // cliente — dopo uno STOP per renderlo invisibile alla guardia. Misurato:
  // uno STOP pulito all'indice 69 non veniva letto perche' ai 3 messaggi
  // successivi seguivano nostre risposte manuali.
  // Ora si leggono gli ultimi N messaggi INBOUND della conversazione, ovunque
  // siano. Costo accettato: uno STOP vecchio e gia' gestito blocca un invio.
  // E' il verso giusto in cui sbagliare — stessa asimmetria della direzione:
  // un invio bloccato di troppo si recupera, un messaggio a chi ha detto STOP no.
  const tail = [];
  for (let i = rows.length - 1; i >= 0 && tail.length < 40; i--) {
    if (direzione(rows[i]) === 'out') continue;   // i nostri non fermano piu'
    tail.push((rows[i].innerText || '').slice(0, 300));
  }
  return tail.reverse();
}
"""
# Spunte di consegna — Q39 RISOLTA il 27/07, ma non come previsto.
# Nessuno dei `data-icon='status-*'` esiste piu' (0 nodi su una chat con 35
# messaggi). Lo stato e' esposto come ARIA: `aria-label` " Consegnato " /
# " Letto " / " In attesa ". Verificato con probe_guardia: aggancia
# `[aria-label*='Consegnato']` su un messaggio inviato davvero.
# I `data-icon` restano in coda come fallback per versioni piu' vecchie.
# ATTENZIONE: e' testo LOCALIZZATO. Su interfaccia non italiana smette di
# funzionare — accettabile in MVP (Q6: solo italiano), da rivedere in M1 se
# il cliente cambia lingua.
TICK_SEL = [
    "[aria-label*='Consegnato']", "[aria-label*='Letto']", "[aria-label*='In attesa']",
    "[data-icon='status-dblcheck']", "[data-icon='status-check']", "[data-icon='status-time']",
]

# Colonne del CSV di audit (A6): una riga "invio" viene scritta SUBITO dopo
# l'Enter (prima di leggere la spunta, che puo' impiegare secondi ed eccepire),
# poi se la lettura della spunta riesce si scrive una seconda riga di
# aggiornamento con lo stesso invio_id. Append-only di proposito: mai riscrivere
# una riga gia' scritta, e' un log di audit su invii reali.
# `inviato` e' 1 SOLO sulla riga evento=invio: la riga evento=spunta la lascia
# vuota, altrimenti un sum() sulla colonna conta due volte lo stesso messaggio (F5).
_CSV_HEADER = [
    "ts", "numero_masked", "invio_id", "evento",
    "open_ms", "guardia_dom_ms", "guardia_totale_ms",
    "inbound_letti", "coda_non_agganciata", "stop_trovato",
    "inviato", "spunta", "totale_ms", "note",
]


class CsvSchemaError(Exception):
    """`send_results.csv` esiste gia' ma il suo header non combacia con lo
    schema atteso (F4): appendere righe nuove sotto un header vecchio le
    disallinea in silenzio. Non riscriviamo/ruotiamo da soli un file di audit:
    ci si ferma e si chiede una decisione umana (rinominare/archiviare il
    file esistente prima di rilanciare)."""


def _scrivi_riga_csv(path: Path, **campi) -> None:
    """Append di una riga; scrive l'header alla primissima riga (file assente
    o presente-ma-vuoto). Se il file esiste gia' CON contenuto, il suo header
    deve combaciare esattamente con `_CSV_HEADER` (F4): 9->14 colonne e' gia'
    successo una volta in questo cantiere."""
    header_atteso = ",".join(_CSV_HEADER)
    new = not path.exists()
    if not new:
        with path.open("r", encoding="utf-8", newline="") as f:
            prima_riga = f.readline().rstrip("\r\n")
        if prima_riga == "":
            new = True   # file esiste ma e' vuoto: l'header non e' mai stato scritto
        elif prima_riga != header_atteso:
            raise CsvSchemaError(
                f"{path} ha un header diverso da quello atteso "
                f"({prima_riga!r} vs {header_atteso!r}). Schema cambiato: "
                f"archivia/rinomina il file esistente prima di rilanciare."
            )
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(_CSV_HEADER)
        w.writerow([campi.get(c, "") for c in _CSV_HEADER])


def _scrivi_riga_csv_protetta(path: Path, evento_fallback: str, **campi) -> None:
    """SOLO per righe scritte dopo un invio reale (Enter gia' premuto): un
    fallimento qui non deve MAI propagare, altrimenti si perde la traccia di
    un messaggio gia' arrivato a una persona vera (F1) — basta che qualcuno
    apra `send_results.csv` in Excel mentre il run gira (lock esclusivo su
    Windows) o che il disco sia pieno. Il fallback va su events.jsonl, file
    indipendente dal CSV: un lock sul CSV non lo tocca."""
    try:
        _scrivi_riga_csv(path, **campi)
    except Exception as exc:
        log_event(evento_fallback, errore=f"{type(exc).__name__}: {exc}"[:200], **campi)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def guardia_pre_invio(page) -> tuple[list[str] | None, float, bool]:
    """Ritorna (ultimi_inbound, millisecondi, stop_trovato).

    `ultimi_inbound` e' None quando JS_TAIL non ha agganciato NESSUNA bolla nel
    DOM (sentinella A2): distinto da lista vuota, che significa "nessun inbound
    da leggere" (caso normale su una chat senza risposte).

    PRIMA di leggere si CARICA la cronologia. La conversazione e' virtualizzata:
    senza scroll nel DOM restano solo i messaggi visibili — misurato il 27/07,
    17 messaggi tutti degli ultimi 3 minuti — e uno STOP di venti minuti prima
    non esiste proprio. La guardia non lo leggeva male: non aveva niente da
    leggere. Il caricamento e' parte della guardia, non un accessorio.
    """
    t0 = time.perf_counter()
    info = await carica_cronologia(page, minimo=80)
    tail = await page.evaluate(JS_TAIL)
    ms = (time.perf_counter() - t0) * 1000
    stop = any(contains_stop(t) for t in tail) if tail is not None else False
    log_event("guardia_cronologia", **info)
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
        # RICERCA, non deep-link (corretto il 27/07). Il deep-link su un numero
        # senza chat ne APRE UNA NUOVA: la guardia V2 la bloccherebbe prima
        # dell'invio, ma la conversazione sarebbe gia' stata creata. La ricerca
        # trova solo cio' che esiste gia', quindi il caso non si presenta
        # affatto — ed e' l'unica delle due strategie verificata sul DOM vero
        # (poc2_open, 3 chat aperte, 8-16s).
        ok, open_ms, segnale = await open_by_search(page, e164)
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
        # ATTENZIONE alla lettura di questo numero (chiarito il 27/07 su misura
        # reale). `guardia_totale_ms` somma l'APERTURA della chat, che con un
        # profilo freddo vale ~17s ed e' dominata da rete e rendering. Ma la
        # chat andrebbe aperta comunque per inviare: non e' costo del controllo
        # opt-out. Il numero da confrontare col criterio GO "guardia <= 2s" e'
        # `guardia_dom_ms`, che misura la lettura della coda inbound: 6 ms sulla
        # prima misura reale. Il totale resta in CSV perche' dice quanto costa
        # un invio END-TO-END, che serve a dimensionare la rampa di M5.
        guardia_totale_ms = open_ms + guardia_dom_ms

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
    ap.add_argument("--messaggio-file", default=str(MESSAGES_FILE),
                    help="file con i messaggi veri scritti da Tommaso (default: %(default)s)")
    ap.add_argument("--send", action="store_true", help="senza questo flag e' dry-run")
    # Il messaggio si sceglie a caso tra quelli non ancora mandati a quel
    # destinatario (rotazione anti-ripetizione). Il TEST NEGATIVO DELLO STOP
    # fa eccezione: richiede il messaggio che chiede "rispondi STOP", quindi
    # deve essere selezionabile. 1-based, come lo si legge in messages.txt.
    ap.add_argument("--messaggio-n", type=int, default=None,
                    help="manda il messaggio n-esimo invece di sceglierlo a caso (1-based)")
    args = ap.parse_args()
    testi = load_messages(args.messaggio_file)   # solleva MessagesFileError se vuoto/malformato (A7)
    if args.messaggio_n is not None:
        if not (1 <= args.messaggio_n <= len(testi)):
            raise SystemExit(f"--messaggio-n fuori range: il file ne ha {len(testi)}.")
        testi = [testi[args.messaggio_n - 1]]
    asyncio.run(main(args.numero, testi, args.send))
