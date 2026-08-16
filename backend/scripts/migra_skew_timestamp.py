"""Sposta di +2 ore i timestamp WhatsApp scritti naive in colonne timestamptz.

IL DIFETTO
    `datetime.utcnow()` e' naive. Scritto in una colonna `DateTime(timezone=True)`
    da un processo con fuso Europe/Rome, viene interpretato come ora LOCALE e
    salvato indietro di 2 ore (d'estate). Misurato il 16/08:

        naive utcnow inviato : 2026-08-16 13:05:21.996993
        riletto come tstz    : 2026-08-16 11:05:20.858123+00:00

PERCHE' UNA MIGRAZIONE, E NON BASTA IL FIX DEL CODICE
    Le righe gia' a DB sono a `reale - 2h`. Appena il codice scrive corretto,
    storico e nuovo diventano incoerenti nella stessa colonna. E' la stessa forma
    del problema phone_hmac: la parte difficile non era unificare la funzione, era
    migrare i dati.

ORDINE OBBLIGATORIO
    coda vuota -> QUESTO SCRIPT -> deploy del codice aware -> restart worker.

    Se il codice va in produzione PRIMA, le righe nuove sono gia' giuste e le
    vecchie no: da quel momento un +2h cieco romperebbe le nuove. Lo script se ne
    accorge da solo (vedi la guardia sotto) e si rifiuta di procedere.

LA GUARDIA
    Prima di scrivere, per ogni colonna misura lo scarto fra la riga PIU' RECENTE
    che verra' toccata e `now()` del server, e cerca la FIRMA dello skew: uno
    scarto vicino a 2h. Se una colonna popolata mostra invece uno scarto molto
    piccolo, quel dato e' gia' corretto -- il codice aware e' gia' in produzione,
    il dataset e' misto, e un +2h cieco romperebbe le righe nuove.

    NON basta "scarto piccolo": `next_action_at` contiene per progetto istanti
    FUTURI (il backoff scrive adesso+6h), quindi il suo scarto e' negativo e
    verrebbe scambiato per "gia' corretta". Le colonne con valori futuri sono
    escluse dall'euristica e segnalate a parte.

    La guardia gira ANCHE con `--fino-a`, misurata sulle sole righe che verranno
    toccate: e' proprio in quel percorso che serve di piu'.

    Resta un'euristica su una riga per colonna, non una prova: leggere sempre il
    dry-run per intero prima di applicare.

NON E' IDEMPOTENTE
    Rilanciarlo somma un altro +2h. Senza `--fino-a` il secondo lancio viene
    intercettato (i massimi diventano futuri), ma CON `--fino-a` no: una riga a
    T-3h diventa T-1h, resta sotto T, e verrebbe spostata di nuovo. Eseguirlo
    una volta sola, e annotare che e' stato fatto.

ROLLBACK
    Dopo la migrazione, un revert del codice aware NON basta: il codice vecchio
    scriverebbe e confronterebbe a reale-2h contro righe a reale, e
    `next_action_at <= now` sarebbe falso per due ore, cioe' il canale
    smetterebbe di inviare. Se si torna indietro sul codice, si torna indietro
    anche sui dati (`--indietro`).

USO
    python scripts/migra_skew_timestamp.py                 # dry-run (default)
    python scripts/migra_skew_timestamp.py --applica       # scrive, chiede conferma
    python scripts/migra_skew_timestamp.py --fino-a "2026-08-16T14:30:00+00:00"
    python scripts/migra_skew_timestamp.py --indietro --applica   # rollback

NOTA SULL'ORA LEGALE
    I dati coperti vanno dal 4 al 16 agosto 2026, interamente dentro l'ora legale:
    +2h uniforme e' corretto per tutto il periodo. Se questa migrazione slitta oltre
    l'ultima domenica di ottobre, il conto va rifatto per le righe invernali.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import make_url

sys.path.insert(0, ".")

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402

SCARTO = "2 hours"

# Tabella -> colonne temporali scritte da Python (quindi storte).
#
# wa_discover_runs e' ESCLUSA di proposito: 0 righe, e il suo codice e' aware
# dal commit 4bd6a7b. Va tenuta fuori da qualunque riscrittura "per tutte le
# wa_*" fatta senza guardare tabella per tabella.
#
# wa_campaign_contacts.locked_at e' inclusa ma di norma vuota (i lock si
# rilasciano): se ci sono righe, sono lock vivi e vanno spostati come il resto.
COLONNE = {
    "wa_messages": ["queued_at", "sent_at"],
    "wa_campaign_contacts": ["next_action_at", "locked_at"],
    "wa_campaigns": ["created_at", "started_at", "completed_at"],
    "wa_numbers": ["created_at", "session_checked_at"],
    "wa_contacts": ["first_seen_at", "last_contacted_at", "last_replied_at",
                    "opted_out_at"],
    "wa_inbound_events": ["detected_at"],
    "wa_discovered_chats": ["discovered_at", "updated_at"],
    "tenants": ["created_at"],
}

# Una colonna e' "gia' corretta" se lo scarto della riga piu' recente da now()
# e' molto piu' piccolo dello skew che stiamo correggendo. Il confronto e' con
# 2h: se lo scarto assomiglia a 2h la colonna e' ancora storta (ci aspettiamo
# quello); se e' vicino a zero, qualcuno ha gia' deployato il codice aware.
SKEW = timedelta(hours=2)
TOLLERANZA_GIA_CORRETTA = timedelta(minutes=45)

# Tabelle che NON si toccano ma si mostrano lo stesso nella fotografia: cosi'
# l'operatore vede coi propri occhi se la premessa dell'esclusione ("0 righe")
# e' ancora vera al momento in cui esegue, invece di fidarsi di un commento.
SOLA_LETTURA = {"wa_discover_runs": ["started_at", "finished_at"]}


async def _fotografia(db, fino_a=None):
    """Per ogni colonna: righe che verranno toccate, min, max, scarto da now().

    Con `fino_a` misura SOLO le righe che l'UPDATE toccherebbe: la guardia deve
    guardare lo stesso insieme su cui si scrive, altrimenti non protegge nulla.
    """
    righe = []
    tutte = [(t, c, False) for t, cols in COLONNE.items() for c in cols]
    tutte += [(t, c, True) for t, cols in SOLA_LETTURA.items() for c in cols]
    for tabella, col, sola_lettura in tutte:
        filtro = ""
        par = {}
        if fino_a and not sola_lettura:
            filtro = f" WHERE {col} < :fino_a"
            par = {"fino_a": fino_a}
        q = await db.execute(text(f"""
            SELECT count({col})              AS n,
                   min({col})                AS minimo,
                   max({col})                AS massimo,
                   now() - max({col})        AS scarto,
                   count(*) FILTER (WHERE {col} > now()) AS futuri
            FROM {tabella}{filtro}
        """), par)
        n, minimo, massimo, scarto, futuri = q.one()
        righe.append({"tabella": tabella, "colonna": col, "n": n,
                      "min": minimo, "max": massimo, "scarto": scarto,
                      "futuri": futuri, "sola_lettura": sola_lettura})
    return righe


def _stampa(titolo, righe):
    print(f"\n=== {titolo} ===")
    print(f"{'tabella':24} {'colonna':20} {'righe':>6}  {'piu recente':26} "
          f"{'scarto da now()':>17}")
    for r in righe:
        massimo = str(r["max"])[:26] if r["max"] else "-"
        scarto = str(r["scarto"]).split(".")[0] if r["scarto"] else "-"
        nota = ""
        if r["sola_lettura"]:
            nota = "  [non toccata]"
        elif r["futuri"]:
            nota = f"  [{r['futuri']} nel futuro]"
        print(f"{r['tabella']:24} {r['colonna']:20} {r['n']:>6}  {massimo:26} "
              f"{scarto:>17}{nota}")


def _guardia(righe):
    """Cerca la firma dello skew. Segnala le colonne che sembrano GIA' corrette.

    Non basta "scarto piccolo": `next_action_at` contiene per progetto istanti
    futuri (il backoff scrive adesso+6h), quindi il suo scarto e' NEGATIVO e
    verrebbe scambiato per "gia' corretta" -- falso allarme che spingerebbe
    l'operatore nel percorso `--fino-a`, che e' quello non idempotente.
    Le colonne con valori futuri sono escluse dall'euristica e riportate a parte.
    """
    gia_corrette, ambigue = [], []
    for r in righe:
        if r["sola_lettura"] or not r["n"] or r["scarto"] is None:
            continue
        if r["futuri"]:
            ambigue.append(r)
            continue
        # Storta = lo scarto assomiglia a >= 2h. Gia' corretta = molto meno.
        if r["scarto"] < SKEW - TOLLERANZA_GIA_CORRETTA:
            gia_corrette.append(r)
    return gia_corrette, ambigue


def _istante_aware(testo: str):
    """Un confine orario senza fuso e' l'errore che questo script combatte."""
    try:
        val = datetime.fromisoformat(testo)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"istante non leggibile: {testo!r}. Usa ISO 8601 col fuso, "
            "es. 2026-08-16T14:30:00+00:00")
    if val.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"istante SENZA fuso: {testo!r}. Un naive verrebbe risolto nel "
            "TimeZone della sessione: e' esattamente la classe di errore che "
            "questo script esiste per rimuovere. Aggiungi l'offset, es. "
            f"{testo}+00:00")
    return val


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--applica", action="store_true",
                    help="scrive davvero (default: dry-run)")
    ap.add_argument("--fino-a", default=None, type=_istante_aware,
                    help="corregge solo le righe con timestamp < ISTANTE (ISO "
                         "8601 CON fuso). Serve se il codice aware e' GIA' in "
                         "produzione: e' il confine fra righe vecchie storte e "
                         "righe nuove giuste. ATTENZIONE: in questo percorso lo "
                         "script non e' idempotente, eseguirlo una volta sola.")
    ap.add_argument("--indietro", action="store_true",
                    help="rollback: sottrae 2 ore invece di aggiungerle. Serve "
                         "se si torna indietro sul codice dopo aver migrato.")
    args = ap.parse_args()

    segno = "-" if args.indietro else "+"
    verso = "rollback (-2h)" if args.indietro else "correzione (+2h)"

    # Su QUALE database si sta per scrivere. Senza questo, il comando
    # documentato lanciato dalla cartella sbagliata punta a un altro DB e
    # l'operatore non ha modo di accorgersene. Stesso pattern di
    # scripts/migrate.py.
    url = make_url(settings.database_url)
    bersaglio = f"{url.drivername} :: {url.host or 'locale'}/{url.database}"
    print(f"DB bersaglio: {bersaglio}")
    if not url.drivername.startswith("postgresql"):
        print("\n!! FERMO: questo script usa now() e interval, e ha senso solo "
              "su PostgreSQL.\n   Il bersaglio non e' PostgreSQL: quasi certamente "
              "stai eseguendo dalla\n   cartella sbagliata (un .env di test "
              "accanto alla CWD).")
        return 2

    async with AsyncSessionLocal() as db:
        prima = await _fotografia(db, args.fino_a)
        _stampa(f"PRIMA ({verso})", prima)

        gia_corrette, ambigue = _guardia(prima)
        if ambigue:
            print("\n   Nota: queste colonne contengono istanti FUTURI (normale: "
                  "il backoff scrive\n   adesso+6h), quindi lo scarto da now() non "
                  "dice nulla sul loro stato:")
            for r in ambigue:
                print(f"   - {r['tabella']}.{r['colonna']}  "
                      f"{r['futuri']} righe nel futuro")
        if gia_corrette:
            print("\n!! FERMO: queste colonne sembrano GIA' corrette "
                  f"(scarto da now() molto minore di {SKEW}):")
            for r in gia_corrette:
                print(f"   - {r['tabella']}.{r['colonna']}  scarto {r['scarto']}")
            print("\n   Significa che il codice aware sta gia' scrivendo righe "
                  "giuste: il dataset e'\n   misto e un +2h cieco romperebbe le "
                  "nuove. Usa --fino-a '<istante del deploy>'\n   per correggere "
                  "solo le righe scritte prima -- ma sappi che in quel percorso\n"
                  "   lo script NON e' idempotente: una riga gia' spostata resta "
                  "sotto il confine\n   e verrebbe spostata una seconda volta. "
                  "Eseguilo una volta sola.")
            return 2

        if not args.applica:
            print("\n[DRY-RUN] nessuna scrittura. Rilancia con --applica per "
                  "scrivere.")
            print("          Leggi la tabella PRIMA qui sopra: sulle colonne "
                  "popolate e senza righe\n          future lo scarto da now() deve "
                  "assomigliare a 2h. Se non lo e', FERMATI.")
            return 0

        print(f"\nSTAI PER SCRIVERE SU: {bersaglio}")
        conferma = input(f"Applicare {segno}{SCARTO} alle colonne elencate? "
                         "scrivi APPLICA: ")
        if conferma.strip() != "APPLICA":
            print("annullato.")
            return 1

        for tabella, colonne in COLONNE.items():
            for col in colonne:
                par = {"fino_a": args.fino_a} if args.fino_a else {}
                res = await db.execute(
                    text(f"UPDATE {tabella} "
                         f"SET {col} = {col} {segno} interval '{SCARTO}'"
                         f" WHERE {col} IS NOT NULL"
                         + (f" AND {col} < :fino_a" if args.fino_a else "")),
                    par)
                print(f"  {tabella}.{col}: {res.rowcount} righe")
        await db.commit()

        dopo = await _fotografia(db)
        _stampa("DOPO", dopo)
        if args.indietro:
            print("\nRollback dei dati fatto. Il codice deve essere quello "
                  "VECCHIO (naive).")
        else:
            print("\nFatto. Ora: deploy del codice aware, poi restart dei worker.")
            print("Annota che questa migrazione e' stata eseguita: NON e' "
                  "idempotente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
