"""G4 (design 08/08, docs/superpowers/specs/2026-08-08-wa-g2g3g5g4-e-auto-
discover-design.md §3.2): disattiva la rampa di warmup del canale WhatsApp,
per DECISIONE DI PRODOTTO di Tommaso -- in questa fase seguira' gli invii
personalmente, la rampa tornera' utile quando il processo sara' automatico.

E' un'operazione sui DATI, non sul codice: `wa_number_manager.
effective_wa_daily_cap()` include il gradino di warmup nel min() SOLO se
`warmup_day > 0` (wa_number_manager.py righe 129-139). Mettere
`warmup_day = 0` su un numero lo esclude dal gradino: il tetto resta il solo
`daily_cap` del numero. `advance_wa_warmup_if_needed()` (chiamata al boot in
app/main.py e ogni giorno dal cron `daily_reset`, vedi task_queue.py) filtra
gia' `WaNumber.warmup_day > 0` nella sua query -- un numero a 0 non avanza
mai da solo, la rampa resta congelata finche' qualcuno non la riaccende a
mano riportando warmup_day a 1.

Nessun gradino maturato va perso: riaccendere la rampa in futuro riparte dal
gradino 1 (comportamento voluto, vedi design doc).

Questo script NON tocca il DB di produzione da solo: stampa sempre lo stato
attuale, e scrive SOLO con --yes esplicito (stesso pattern di
scripts/wa_purge_tenant.py). Tommaso applica il cambiamento a mano quando
decide.

CLI:
    python -m scripts.wa_disable_warmup [--tenant-id <uuid>] [--dry-run] [--yes]

--dry-run (o nessun flag): stampa i numeri che verrebbero toccati (quelli con
    warmup_day != 0) e il loro warmup_day/daily_cap attuali. NON scrive nulla.
    E' anche il comportamento di default.
--yes: scrittura REALE, warmup_day = 0 su ogni WaNumber toccato (tutti, o solo
    quelli del tenant se --tenant-id e' passato). warmup_advanced_date NON
    viene toccato: non serve, la query di advance_wa_warmup_if_needed esclude
    gia' i numeri con warmup_day <= 0 a prescindere da quella colonna.
--tenant-id: limita l'operazione a un solo tenant. Senza, opera su TUTTI i
    numeri: la decisione di Tommaso e' di prodotto, non per-cliente.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

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
                  "disattivata (o non ci sono numeri nel filtro dato).")
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
            return

        for n in da_toccare:
            n.warmup_day = 0
        await db.commit()
        print(f"\nFatto: warmup_day=0 su {len(da_toccare)} numero/i. "
              "warmup_advanced_date lasciato com'era (irrilevante quando "
              "warmup_day<=0: advance_wa_warmup_if_needed esclude questi "
              "numeri dalla sua query a prescindere).")


if __name__ == "__main__":
    asyncio.run(main())
