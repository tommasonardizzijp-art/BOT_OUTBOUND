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
    e `now()` del server. Una colonna ancora storta ha uno scarto >= ~2h; una
    colonna gia' corretta ha uno scarto di minuti. Se trova una colonna gia'
    corretta, si ferma: quel dataset e' misto e va trattato con `--fino-a`.

    La guardia e' euristica su una riga sola, non una prova: leggere sempre il
    dry-run per intero prima di applicare.

USO
    python scripts/migra_skew_timestamp.py                 # dry-run (default)
    python scripts/migra_skew_timestamp.py --applica       # scrive, chiede conferma
    python scripts/migra_skew_timestamp.py --fino-a "2026-08-16 14:30:00+00"

NOTA SULL'ORA LEGALE
    I dati coperti vanno dal 4 al 16 agosto 2026, interamente dentro l'ora legale:
    +2h uniforme e' corretto per tutto il periodo. Se questa migrazione slitta oltre
    l'ultima domenica di ottobre, il conto va rifatto per le righe invernali.
"""
import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import text

sys.path.insert(0, ".")

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

# Sotto questa soglia una colonna e' considerata GIA' corretta: la riga piu'
# recente e' vicina a now(), quindi qualcuno ha gia' deployato il codice aware.
SOGLIA_GIA_CORRETTA = timedelta(minutes=45)


async def _fotografia(db):
    """Per ogni colonna: righe valorizzate, min, max, scarto del max da now()."""
    righe = []
    for tabella, colonne in COLONNE.items():
        for col in colonne:
            q = await db.execute(text(f"""
                SELECT count({col}) AS n,
                       min({col})   AS minimo,
                       max({col})   AS massimo,
                       now() - max({col}) AS scarto
                FROM {tabella}
            """))
            n, minimo, massimo, scarto = q.one()
            righe.append({"tabella": tabella, "colonna": col, "n": n,
                          "min": minimo, "max": massimo, "scarto": scarto})
    return righe


def _stampa(titolo, righe):
    print(f"\n=== {titolo} ===")
    print(f"{'tabella':24} {'colonna':20} {'righe':>6}  {'piu recente':26} scarto da now()")
    for r in righe:
        massimo = str(r["max"])[:26] if r["max"] else "-"
        scarto = str(r["scarto"]).split(".")[0] if r["scarto"] else "-"
        print(f"{r['tabella']:24} {r['colonna']:20} {r['n']:>6}  {massimo:26} {scarto}")


def _guardia(righe, fino_a):
    """Rifiuta il dataset misto: righe gia' corrette insieme a righe storte."""
    if fino_a:
        return []
    sospette = [r for r in righe
                if r["n"] and r["scarto"] is not None
                and r["scarto"] < SOGLIA_GIA_CORRETTA]
    return sospette


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--applica", action="store_true",
                    help="scrive davvero (default: dry-run)")
    ap.add_argument("--fino-a", default=None,
                    help="corregge solo le righe con timestamp < ISTANTE. "
                         "Serve se il codice aware e' GIA' in produzione: e' il "
                         "confine fra righe vecchie storte e righe nuove giuste.")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        prima = await _fotografia(db)
        _stampa("PRIMA", prima)

        sospette = _guardia(prima, args.fino_a)
        if sospette:
            print("\n!! FERMO: queste colonne sembrano GIA' corrette "
                  f"(scarto da now() < {SOGLIA_GIA_CORRETTA}):")
            for r in sospette:
                print(f"   - {r['tabella']}.{r['colonna']}  scarto {r['scarto']}")
            print("\n   Significa che il codice aware e' gia' in produzione e sta "
                  "scrivendo righe giuste.\n   Un +2h cieco le romperebbe. Rilancia "
                  "con --fino-a '<istante del deploy>' per\n   correggere solo le "
                  "righe scritte prima.")
            return 2

        if not args.applica:
            print("\n[DRY-RUN] nessuna scrittura. Rilancia con --applica per "
                  "scrivere.")
            print("          Leggi la tabella PRIMA qui sopra: lo scarto da now() "
                  "deve essere\n          ~2h su ogni colonna popolata. Se non lo e', "
                  "FERMATI e capisci perche'.")
            return 0

        conferma = input(f"\nApplicare +{SCARTO} a tutte le colonne elencate? "
                         "scrivi APPLICA: ")
        if conferma.strip() != "APPLICA":
            print("annullato.")
            return 1

        for tabella, colonne in COLONNE.items():
            for col in colonne:
                par = {"fino_a": args.fino_a} if args.fino_a else {}
                res = await db.execute(
                    text(f"UPDATE {tabella} SET {col} = {col} + interval '{SCARTO}'"
                         f" WHERE {col} IS NOT NULL"
                         + (f" AND {col} < :fino_a" if args.fino_a else "")),
                    par)
                print(f"  {tabella}.{col}: {res.rowcount} righe")
        await db.commit()

        dopo = await _fotografia(db)
        _stampa("DOPO", dopo)
        print("\nFatto. Ora: deploy del codice aware, poi restart dei worker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
