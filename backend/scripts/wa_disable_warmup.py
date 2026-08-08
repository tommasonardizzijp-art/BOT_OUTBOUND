"""G4 (design 08/08, docs/superpowers/specs/2026-08-08-wa-g2g3g5g4-e-auto-
discover-design.md §3.2): disattiva la rampa di warmup del canale WhatsApp,
per DECISIONE DI PRODOTTO di Tommaso -- in questa fase seguira' gli invii
personalmente, la rampa tornera' utile quando il processo sara' automatico.

AGGIORNATO (08/08, dopo l'introduzione del flag `WA_WARMUP_ENABLED`): questo
script da SOLO non e' piu' sufficiente a "spegnere" la rampa in modo che
resti spenta. Storia del perche':

1. Prima versione: mettere `warmup_day = 0` su un numero lo esclude dal
   gradino (`wa_number_manager.effective_wa_daily_cap()` includeva il
   gradino nel min() solo se `warmup_day > 0`). Funzionava, MA...
2. ...si e' scoperto che `POST /wa/numbers/{id}/riattiva`
   (`app/api/wa_numbers.py`) scrive `warmup_day = 1` INCONDIZIONATAMENTE ad
   ogni riattivazione (comportamento voluto e non toccato: un numero
   sospeso non deve ripartire dal cap alto a cui era arrivato). Un numero
   con la rampa spenta via `warmup_day=0` che passa per sospensione e
   riattivazione la RIACCENDE in silenzio.
3. Per questo esiste ORA `settings.wa_warmup_enabled` (default True,
   config.py): un interruttore GLOBALE che `effective_wa_daily_cap()` e
   `advance_wa_warmup_if_needed()` controllano IN AND col valore di
   `warmup_day`, qualunque esso sia. E' un flag di CONFIGURAZIONE
   (`.env`/env var), non una colonna a DB: nessuno script Python che tocca
   solo il database puo' scriverlo per davvero (servirebbe riavviare il
   processo con `WA_WARMUP_ENABLED=false` nell'ambiente).

Questo script continua a occuparsi SOLO della parte dati (`warmup_day = 0`
sui numeri, operazione ancora utile: riparte pulita quando la rampa verra'
riaccesa, e tiene il gradino fuori dal min() anche se qualcuno dimenticasse
il flag). Ma stampa SEMPRE un promemoria esplicito che la disattivazione
NON e' completa/robusta a una futura riattivazione finche' non si imposta
ANCHE `WA_WARMUP_ENABLED=false` nella configurazione del processo (e si
riavvia) -- quella parte e' fuori dalla portata di uno script sul DB.

Nessun gradino maturato va perso: riaccendere la rampa in futuro riparte dal
gradino 1 (comportamento voluto, vedi design doc).

Questo script NON tocca il DB di produzione da solo: stampa sempre lo stato
attuale, e scrive SOLO con --yes esplicito (stesso pattern di
scripts/wa_purge_tenant.py). Tommaso applica il cambiamento a mano quando
decide.

CLI:
    python -m scripts.wa_disable_warmup [--tenant-id <uuid>] [--dry-run] [--yes]

--dry-run (o nessun flag): stampa i numeri che verrebbero toccati (quelli con
    warmup_day != 0), il loro warmup_day/daily_cap attuali, e lo stato ATTUALE
    di WA_WARMUP_ENABLED letto dalla configurazione di questo processo. NON
    scrive nulla. E' anche il comportamento di default.
--yes: scrittura REALE, warmup_day = 0 su ogni WaNumber toccato (tutti, o solo
    quelli del tenant se --tenant-id e' passato). warmup_advanced_date NON
    viene toccato: non serve, la query di advance_wa_warmup_if_needed esclude
    gia' i numeri con warmup_day <= 0 (o con la rampa disabilitata dal flag)
    a prescindere da quella colonna.
--tenant-id: limita l'operazione a un solo tenant. Senza, opera su TUTTI i
    numeri: la decisione di Tommaso e' di prodotto, non per-cliente.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.wa import WaNumber


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant-id", default=None,
                   help="Limita ai numeri di un solo tenant. Default: tutti.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true",
                   help="Richiesto ESPLICITAMENTE per la scrittura reale.")
    return p.parse_args()


def _promemoria_flag() -> str:
    if settings.wa_warmup_enabled:
        return (
            "\n*** WA_WARMUP_ENABLED e' attualmente TRUE (default) su questo "
            "processo: mettere warmup_day=0 sui numeri tiene il gradino fuori "
            "dal min() OGGI, ma una futura riattivazione (POST /wa/numbers/"
            "{id}/riattiva) scrive warmup_day=1 incondizionatamente e la "
            "rampa torna a comandare -- QUESTO SCRIPT NON BASTA da solo per "
            "una disattivazione robusta. Per quella serve impostare "
            "WA_WARMUP_ENABLED=false nella configurazione del processo "
            "(.env / env var) e riavviare: e' fuori dalla portata di uno "
            "script sul DB. ***")
    return ("\nWA_WARMUP_ENABLED e' gia' FALSE su questo processo: la rampa "
            "e' disattivata a livello globale, indipendentemente da "
            "warmup_day. Questo script resta utile solo per far ripartire "
            "puliti i gradini quando la rampa verra' riaccesa in futuro.")


async def main() -> None:
    args = _parse_args()
    # Stesso principio di wa_purge_tenant.py: --yes da solo e' l'unico modo
    # di scrivere. Qualunque altra combinazione (niente flag, --dry-run,
    # --dry-run insieme a --yes) resta sul lato sicuro.
    dry_run = args.dry_run or not args.yes

    async with AsyncSessionLocal() as db:
        query = select(WaNumber)
        if args.tenant_id:
            query = query.where(WaNumber.tenant_id == args.tenant_id)
        numeri = (await db.execute(query)).scalars().all()

        da_toccare = [n for n in numeri if (n.warmup_day or 0) != 0]

        if not da_toccare:
            print("Nessun numero con warmup_day != 0: la rampa e' gia' "
                  "disattivata sui dati (o non ci sono numeri nel filtro dato).")
            print(_promemoria_flag())
            return

        print(f"{'[dry-run] ' if dry_run else ''}Numeri con la rampa ATTIVA "
              f"({len(da_toccare)} su {len(numeri)} totali):")
        for n in da_toccare:
            print(f"  {n.id} ({n.label!r}, status={n.status.value}): "
                  f"warmup_day={n.warmup_day} -> 0, daily_cap={n.daily_cap} "
                  "(dopo lo spegnimento e' l'UNICO tetto rimasto)")

        if dry_run:
            print("\nNESSUNA modifica scritta. Per applicarla per davvero, "
                  "ripeti il comando con --yes esplicito (nessun altro flag basta).")
            print(_promemoria_flag())
            return

        for n in da_toccare:
            n.warmup_day = 0
        await db.commit()
        print(f"\nFatto: warmup_day=0 su {len(da_toccare)} numero/i. "
              "warmup_advanced_date lasciato com'era (irrilevante quando "
              "warmup_day<=0 o rampa disabilitata dal flag: "
              "advance_wa_warmup_if_needed esclude questi numeri dalla sua "
              "query a prescindere).")
        print(_promemoria_flag())


if __name__ == "__main__":
    asyncio.run(main())
