# backend/scripts/poc_wa/poc4_coexist.py
"""PoC-4 — il bot e l'umano sullo stesso numero, contemporaneamente.

Quattro scenari, tutti su chat controllate. Tommaso tiene in mano il telefono del
numero mittente (fa l'umano-business); il "contatto" e' una delle chat in allowlist.

  S1  bot invia mentre l'umano ha WhatsApp aperto sul telefono (chat diversa)
  S2  bot invia mentre l'umano ha APERTA LA STESSA chat            -> Q75
  S3  l'umano scrive nella chat 1-2 secondi prima che il bot invii -> check C4
  S4  l'umano legge la risposta prima dello scan del bot           -> Q73

Non automatizza l'umano: e' un cronometro con protocollo. Lo script chiede
conferma a schermo tra uno scenario e l'altro.

Uso:  python poc4_coexist.py --numero "+39..." [--messaggio-file D:\\dev\\wa-poc\\messages.txt] --scenario S1
"""
import argparse
import asyncio
import csv
import random
from datetime import datetime, timezone

from _common import MESSAGES_FILE, artifacts_dir, first_locator, human_type, log_event, snap, wa_context
from poc2_open import COMPOSER_SEL, open_by_deeplink
from poc2_send import guardia_pre_invio, leggi_spunta
from poc_state import OptOutStore
from wa_lib import AllowList, load_messages, normalize_e164

ISTRUZIONI = {
    "S1": "Apri WhatsApp sul telefono su una chat DIVERSA e tienilo acceso. Poi premi Invio qui.",
    "S2": "Apri sul telefono ESATTAMENTE la chat di destinazione e tienila aperta. Poi premi Invio qui.",
    "S3": "Scrivi tu un messaggio in quella chat dal telefono, poi entro 2 secondi premi Invio qui.",
    "S4": "Fai scrivere una risposta dal secondo telefono, LEGGILA dal telefono del secondario, poi premi Invio qui.",
}


def _scrivi_riga_csv(scenario: str, esito: str, dettaglio: str) -> None:
    path = artifacts_dir() / "coexist_results.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "scenario", "esito", "dettaglio"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), scenario, esito, dettaglio])


async def main(numero: str, messaggio: str, scenario: str) -> None:
    allow = AllowList.load()
    e164 = normalize_e164(numero)
    allow.assert_allowed(e164)

    # Guardia opt-out (stesso ordine di poc2_send.py, A1 punto 1): un numero
    # gia' in opt-out non si vede aprire nemmeno la chat. Controllato PRIMA del
    # protocollo a schermo (e fuori da wa_context): non ha senso far mettere
    # Tommaso in posizione col telefono per uno scenario che va comunque
    # saltato. `e164` e' gia' normalizzato: OptOutStore non normalizza le
    # chiavi (contratto in poc_state.py), passargli qualcos'altro
    # significherebbe registrare/cercare un opt-out su una chiave sbagliata.
    optout = OptOutStore.load()
    if optout.is_opted_out(e164):
        esito = "skip-optout-preesistente"
        dettaglio = "numero gia' in opt-out: nessuna chat aperta, nessun invio"
        _scrivi_riga_csv(scenario, esito, dettaglio)
        log_event("coexist_skip_optout", scenario=scenario, numero_masked=f"…{e164[-4:]}")
        print(f"Numero gia' in opt-out: {dettaglio}")
        return

    print(f"\n=== {scenario} ===\n{ISTRUZIONI[scenario]}")
    input("> ")

    async with wa_context(headless=False) as (context, page):
        ok, _, segnale = await open_by_deeplink(page, e164)
        if not ok:
            raise SystemExit(f"Chat non aperta: {segnale}")
        tail, guardia_ms, stop = await guardia_pre_invio(page)
        # A5/A2: tail e' None quando JS_TAIL non ha agganciato NESSUNA bolla
        # (sentinella), distinto dalla lista vuota (nessun inbound dopo il
        # nostro ultimo messaggio). len(tail) su None crasherebbe a meta'
        # scenario; stesso trattamento di poc2_send.py: coda non agganciata =>
        # non si invia.
        coda_non_agganciata = tail is None
        inbound_letti = len(tail) if tail is not None else 0

        # Uno STOP visto nella coda si registra SEMPRE (come poc2_send.py fa
        # indipendentemente dal flag --send), non solo negli scenari che
        # inviano: e' un net di sicurezza, non un dettaglio dello scenario in
        # corso. Va distinto nel CSV da "coda non agganciata": il primo e'
        # "ho letto la coda e c'era uno STOP", il secondo e' "non ho letto
        # niente, non so se c'era".
        if stop:
            optout.add(e164, motivo="STOP rilevato nella coda inbound pre-invio (poc4_coexist)")

        dettaglio = (f"inbound_in_coda={inbound_letti} "
                     f"coda_non_agganciata={int(coda_non_agganciata)} "
                     f"guardia_ms={round(guardia_ms)} stop={stop}")

        if scenario == "S4":
            # Non si invia: si osserva solo se un inbound gia' letto dall'umano
            # resta rilevabile (il badge unread sparisce -> il watcher lo perde?).
            await snap(page, "poc4-S4-dopo-lettura-umana")
            esito = "osservato"
        elif coda_non_agganciata:
            esito = "invio-annullato-coda-non-agganciata"
        elif stop:
            esito = "invio-annullato-da-STOP"
        else:
            comp = await first_locator(page, COMPOSER_SEL, timeout_ms=10000)
            if not comp:
                await snap(page, "poc4-composer-non-trovato")
                raise SystemExit("Composer non trovato: catalogare il selettore prima di riprovare.")
            await human_type(page, comp[0], messaggio)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)
            dettaglio += f" spunta={await leggi_spunta(page)}"
            esito = "inviato"
        await snap(page, f"poc4-{scenario}")

        _scrivi_riga_csv(scenario, esito, dettaglio)
        log_event("coexist", scenario=scenario, esito=esito, dettaglio=dettaglio)

    print("\nOra GUARDA IL TELEFONO e annota nel report:")
    print(" - il messaggio e' comparso mentre guardavi? come si vede?")
    print(" - il telefono ha mostrato notifiche/anomalie? la sessione e' rimasta collegata?")
    print(" - c'e' stato un doppio messaggio o un ordine strano?")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", required=True)
    ap.add_argument("--messaggio-file", default=str(MESSAGES_FILE),
                    help="default: %(default)s")
    ap.add_argument("--scenario", required=True, choices=["S1", "S2", "S3", "S4"])
    args = ap.parse_args()
    # A5/A7: parsing condiviso con poc2_send.py (wa_lib.load_messages), non piu'
    # riga-per-riga: solleva MessagesFileError leggibile se il file e' vuoto o
    # assente, invece di un IndexError su random.choice([]).
    messaggi = load_messages(args.messaggio_file)
    asyncio.run(main(args.numero, random.choice(messaggi), args.scenario))
