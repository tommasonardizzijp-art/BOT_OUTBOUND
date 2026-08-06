"""Purge GDPR per-tenant del canale WhatsApp (SDD S12.4): "il cliente X chiude
il rapporto" deve poter cancellare OGNI sua riga nelle tabelle wa_* + il
profilo browser Chromium di ogni suo numero, in un'unica operazione atomica.

Nessuna tabella wa_* ha ondelete=CASCADE (verificato in app/models/wa.py,
grep di tutti i ForeignKey verso tenants/wa_numbers/wa_contacts/wa_campaigns):
l'ordine di cancellazione sotto e' scelto a mano rispettando le FK reali.
wa_sequence_steps e' INCLUSA anche se non elencata nell'handoff originale --
e' figlia di wa_campaigns esattamente come wa_campaign_contacts e wa_messages,
e senza CASCADE cancellare wa_campaigns prima di lei fa fallire il DB
(IntegrityError su Postgres, "FOREIGN KEY constraint failed" su SQLite con
PRAGMA foreign_keys=ON, attivo su ogni connessione di test).

Sicurezza sul profilo browser: MAI un mirror-delete ricorsivo che segua
reparse point/junction NTFS. La notte 05-06/08 un `robocopy /MIR` senza `/XJ`
ha seguito una junction e cancellato un profilo WhatsApp REALE al di fuori
della cartella prevista (incidente su questo stesso progetto). Qui si usa
`shutil.rmtree` SOLO su directory vere; se il path del profilo e' esso stesso
una junction/symlink si rimuove il solo collegamento con `os.rmdir` (mai il
target). Verificato sperimentalmente con Python 3.13.7 su questa macchina
(vedi docstring di `remove_profile_dir_safely`) -- non e' un'assunzione.

CLI:
    python -m scripts.wa_purge_tenant --tenant-id <uuid> [--dry-run] [--yes]

--dry-run (o nessun flag): stampa i conteggi di cio' che verrebbe cancellato,
    NON cancella nulla. E' anche il comportamento di default.
--yes: cancellazione REALE, atomica (una singola sessione/transazione: se
    qualcosa fallisce a meta', il commit non parte e nessuna riga resta
    cancellata a meta').
"""
import argparse
import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

from app.database import AsyncSessionLocal
from app.models.tenant import Tenant
from app.models.wa import (WaCampaign, WaCampaignContact, WaContact,
                           WaInboundEvent, WaMessage, WaNumber, WaSequenceStep)
from app.services.wa_session import profile_dir_for

# Ordine di stampa/cancellazione FK-safe: figli prima dei genitori.
_TABELLE = ("wa_inbound_events", "wa_messages", "wa_campaign_contacts",
           "wa_sequence_steps", "wa_campaigns", "wa_contacts", "wa_numbers")


def _is_reparse_point(path: Path) -> bool:
    """True se `path` e' una junction NTFS o un symlink (Windows o POSIX).

    Misurato con questo Python (3.13.7) su una junction NTFS vera creata con
    `mklink /J`: os.path.islink() da SOLO ritorna False su una junction -- non
    e' un segnale affidabile su Windows. Il segnale che funziona e'
    FILE_ATTRIBUTE_REPARSE_POINT sull'os.lstat() (st_file_attributes, presente
    solo su Windows). Su POSIX os.path.islink() gia' basta per un vero symlink.
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    if os.path.islink(path):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def remove_profile_dir_safely(path: Path) -> None:
    """Cancella la directory del profilo browser di UN numero, mai il target
    di una junction/symlink verso cui puntasse. Idempotente: path assente =
    no-op (numero mai onboardato).

    Due casi, entrambi verificati sperimentalmente su questa macchina prima di
    scrivere questa funzione (non per assunzione):

    1. `path` e' essa stessa una junction/symlink: `shutil.rmtree` si RIFIUTA
       di procedere (solleva OSError "Cannot call rmtree on a symbolic link")
       invece di attraversarla -- ma non la rimuove nemmeno. Va tolta a mano:
       `os.rmdir` su un reparse-point-directory rimuove SOLO il collegamento,
       il contenuto del target resta intatto (provato con un file canary
       dentro il target: sopravvive).
    2. `path` e' una directory vera, anche con junction ANNIDATE al suo
       interno: `shutil.rmtree` e' sicuro in questo caso, cancella l'albero
       reale e sulle junction annidate incontrate durante la scansione si
       ferma al collegamento senza mai scendere nel loro target (stesso test
       col canary, superato).
    """
    if not os.path.lexists(path):
        return
    if _is_reparse_point(path):
        try:
            os.rmdir(path)
        except NotADirectoryError:
            os.unlink(path)
    else:
        shutil.rmtree(path)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true",
                   help="Richiesto ESPLICITAMENTE per la cancellazione reale.")
    return p.parse_args()


async def _campaign_ids(db, tenant_id: str) -> list[str]:
    r = await db.execute(select(WaCampaign.id).where(WaCampaign.tenant_id == tenant_id))
    return [row[0] for row in r.all()]


async def _number_ids(db, tenant_id: str) -> list[str]:
    r = await db.execute(select(WaNumber.id).where(WaNumber.tenant_id == tenant_id))
    return [row[0] for row in r.all()]


async def _count_in(db, model, column, values: list[str]) -> int:
    if not values:
        return 0
    return await db.scalar(
        select(func.count()).select_from(model).where(column.in_(values))) or 0


async def _count_tenant(db, model, tenant_id: str) -> int:
    return await db.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)) or 0


async def _compute_counts(db, tenant_id: str, campaign_ids: list[str]) -> dict:
    return {
        "wa_inbound_events": await _count_tenant(db, WaInboundEvent, tenant_id),
        "wa_messages": await _count_in(db, WaMessage, WaMessage.campaign_id, campaign_ids),
        "wa_campaign_contacts": await _count_in(
            db, WaCampaignContact, WaCampaignContact.campaign_id, campaign_ids),
        "wa_sequence_steps": await _count_in(
            db, WaSequenceStep, WaSequenceStep.campaign_id, campaign_ids),
        "wa_campaigns": await _count_tenant(db, WaCampaign, tenant_id),
        "wa_contacts": await _count_tenant(db, WaContact, tenant_id),
        "wa_numbers": await _count_tenant(db, WaNumber, tenant_id),
    }


def _print_counts(counts: dict, tenant: Tenant) -> None:
    for tabella in _TABELLE:
        print(f"  {tabella}: {counts[tabella]}")
    print(f"  tenant: 1 ({tenant.name})")


async def main() -> None:
    args = _parse_args()
    # Cancellazione REALE solo se --yes e' l'UNICO segnale d'intento: se
    # manca --yes, o se --dry-run e' passato insieme (contraddizione), vince
    # il default sicuro (dry-run). --yes da solo e' l'unico modo di cancellare.
    dry_run = args.dry_run or not args.yes

    async with AsyncSessionLocal() as db:
        tenant = await db.get(Tenant, args.tenant_id)
        if tenant is None:
            print(f"ERRORE: nessun tenant con id={args.tenant_id!r}. "
                  "Nessuna modifica al DB.", file=sys.stderr)
            raise SystemExit(1)

        campaign_ids = await _campaign_ids(db, args.tenant_id)
        number_ids = await _number_ids(db, args.tenant_id)
        counts = await _compute_counts(db, args.tenant_id, campaign_ids)

        if dry_run:
            print(f"[dry-run] tenant {tenant.id} ({tenant.name}): NESSUNA "
                  "cancellazione. Conteggi di cio' che verrebbe cancellato:")
            _print_counts(counts, tenant)
            print("Per la cancellazione REALE, ripeti il comando con --yes "
                  "esplicito (nessun altro flag basta).")
            return

        print(f"Cancellazione REALE per tenant {tenant.id} ({tenant.name}):")
        _print_counts(counts, tenant)

        # Ordine FK-safe (figli prima dei genitori), tutto nella STESSA
        # sessione/transazione: il commit e' un solo punto, in fondo. Se una
        # delete a meta' solleva, non si arriva al commit e SQLAlchemy fa
        # rollback della sessione alla chiusura del `async with` -- nessuna
        # riga resta cancellata a meta'.
        await db.execute(delete(WaInboundEvent).where(WaInboundEvent.tenant_id == args.tenant_id))
        if campaign_ids:
            await db.execute(delete(WaMessage).where(WaMessage.campaign_id.in_(campaign_ids)))
            await db.execute(
                delete(WaCampaignContact).where(WaCampaignContact.campaign_id.in_(campaign_ids)))
            await db.execute(delete(WaSequenceStep).where(WaSequenceStep.campaign_id.in_(campaign_ids)))
        await db.execute(delete(WaCampaign).where(WaCampaign.tenant_id == args.tenant_id))
        await db.execute(delete(WaContact).where(WaContact.tenant_id == args.tenant_id))
        await db.execute(delete(WaNumber).where(WaNumber.tenant_id == args.tenant_id))
        await db.execute(delete(Tenant).where(Tenant.id == args.tenant_id))
        await db.commit()

    # Filesystem FUORI dalla transazione DB, di proposito: eseguito solo DOPO
    # il commit riuscito, cosi' un rollback del DB non lascia mai un profilo
    # vero gia' cancellato mentre le sue righe sopravvivono (o viceversa).
    for number_id in number_ids:
        remove_profile_dir_safely(profile_dir_for(number_id))

    print(f"Purge completato: {len(number_ids)} profili browser rimossi (o gia' assenti).")


if __name__ == "__main__":
    asyncio.run(main())
