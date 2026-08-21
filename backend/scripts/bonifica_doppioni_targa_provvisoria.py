"""Fonde i doppioni nati dall'incontro fra canale browser e canale API sull'inbox.

IL DIFETTO (misurato su prod il 21/08/2026, campagna AV X @michele.carozza): il
canale browser non conosce il pk Instagram e salva una TARGA PROVVISORIA negativa
(`app/services/inbox_browser/targa.py`); il percorso API deduplicava solo su
`ig_user_id`. Le due chiavi non combaciano mai, quindi l'API reinseriva come
"nuovo" ogni contatto che il browser aveva gia' preso: 32 righe gemelle su 34.

Il buco e' chiuso in `scrape_inbox.classifica_pagina` (l'API ora promuove la riga
esistente invece di duplicarla). Questo script ripara le righe gia' create.

REGOLA DI FUSIONE — si tiene la riga del BROWSER, non quella dell'API:
la riga browser porta `full_name`, `last_message_at/from/text`; la gemella API
nasce con quei campi vuoti. Quindi si scrive il pk vero sulla riga browser e si
cancella la gemella API. Nessun dato viene sovrascritto: la riga API non ne ha.

GUARDIE (una coppia che non le rispetta tutte viene SALTATA e riportata, mai
"aggiustata a intuito"):
  - esattamente 2 righe per username nella campagna;
  - una con targa provvisoria (<0) e una con targa reale (>0);
  - la riga API da cancellare deve essere davvero una scheda vuota: stato
    `pending`, nessun messaggio collegato, nessun lock, nessuno dei campi in
    CAMPI_ARRICCHIMENTO (dati di bio/contatto E i campi del canale browser).

Uso (dal folder backend), sempre con --campaign salvo eccezioni dichiarate:
    ./venv/Scripts/python.exe scripts/bonifica_doppioni_targa_provvisoria.py --campaign <id>
    ./venv/Scripts/python.exe scripts/bonifica_doppioni_targa_provvisoria.py --campaign <id> --apply
Stampa in testa il database su cui sta lavorando: leggilo prima di dare --apply.

Senza `--apply` non scrive niente. Con `--apply` scrive PRIMA un backup JSON di
tutte le righe coinvolte (opzione --backup per il percorso, default
`backups/bonifica_doppioni_<timestamp>.json`) e poi lavora una coppia per
transazione: se una fallisce, le altre restano valide.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select, update

from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.follower import Follower, FollowerStatus
from app.models.message import Message
from app.services.inbox_browser.targa import e_provvisoria, normalizza_username

# Campi la cui presenza dice "questa riga ha una storia": se la gemella con targa
# reale ne ha anche uno solo, non e' la scheda vuota appena creata dall'API e non si
# cancella. Include i campi del canale browser (full_name, last_message_*,
# source_channel): sono esattamente quelli per cui la riga browser vince la fusione,
# quindi trovarli sulla riga da cancellare significa che le due righe non sono
# quello che questo script crede, e la coppia va guardata a mano.
CAMPI_ARRICCHIMENTO = (
    "biography", "phone", "email", "whatsapp", "bio_links", "contact_source",
    "external_url", "follower_count",
    "full_name", "last_message_at", "last_message_from", "last_message_text",
    "source_channel",
)


def _db_mascherato() -> str:
    """L'URL del database senza la password."""
    from app.config import settings
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", settings.database_url or "?")


def _riga_dict(f: Follower) -> dict:
    """La riga intera, serializzabile: e' il backup, deve poterla ricreare."""
    out = {}
    for colonna in Follower.__table__.columns:
        valore = getattr(f, colonna.name)
        if isinstance(valore, datetime):
            valore = valore.isoformat()
        elif isinstance(valore, FollowerStatus):
            valore = valore.value
        out[colonna.name] = valore
    return out


async def _coppie(db, campaign_id: str | None):
    """Tutte le coppie (riga_browser, riga_api) candidate alla fusione."""
    q = select(Follower)
    if campaign_id:
        q = q.where(Follower.campaign_id == campaign_id)
    righe = (await db.execute(q)).scalars().all()

    per_chiave: dict[tuple[str, str], list[Follower]] = {}
    for r in righe:
        u = normalizza_username(r.username) if isinstance(r.username, str) else ""
        if not u:
            continue
        per_chiave.setdefault((r.campaign_id, u), []).append(r)

    coppie, scartate = [], []
    for (cid, u), gruppo in per_chiave.items():
        if len(gruppo) < 2:
            continue
        provvisorie = [r for r in gruppo if e_provvisoria(r.ig_user_id)]
        reali = [r for r in gruppo if not e_provvisoria(r.ig_user_id)]
        if len(gruppo) != 2 or len(provvisorie) != 1 or len(reali) != 1:
            scartate.append((cid, u, f"{len(gruppo)} righe ({len(provvisorie)} provvisorie, "
                                     f"{len(reali)} reali): non e' la coppia attesa"))
            continue
        coppie.append((cid, u, provvisorie[0], reali[0]))
    return coppie, scartate


async def _api_e_una_scheda_vuota(db, riga: Follower) -> str | None:
    """None se la riga API si puo' cancellare, altrimenti il motivo per cui no."""
    if riga.status != FollowerStatus.pending:
        return f"stato '{riga.status.value}' (non pending): la riga ha gia' una storia"
    if riga.locked_by_account_id:
        return "lock attivo: un worker la sta lavorando"
    for campo in CAMPI_ARRICCHIMENTO:
        if getattr(riga, campo, None):
            return f"ha gia' il dato '{campo}': non e' una scheda vuota"
    n_msg = await db.scalar(
        select(func.count(Message.id)).where(Message.follower_id == riga.id)
    ) or 0
    if n_msg:
        return f"{n_msg} messaggi collegati (verrebbero cancellati in CASCADE)"
    return None


async def fondi_coppia(db, browser_row: Follower, api_row: Follower) -> str:
    """Fonde UNA coppia. Ritorna 'fusa' oppure il motivo per cui non lo e'.

    Ordine obbligato: prima la DELETE della gemella, poi la UPDATE della riga
    browser — invertendoli la UPDATE violerebbe UNIQUE(campaign_id, ig_user_id)
    contro la gemella ancora viva.

    La UPDATE tiene la guardia `ig_user_id < 0` e ne CONTROLLA l'esito: se nel
    frattempo qualcuno ha promosso o cancellato la riga browser, senza il controllo
    la DELETE passerebbe lo stesso e il contatto sparirebbe insieme al suo pk.
    """
    pk_vero = api_row.ig_user_id
    await db.execute(delete(Follower).where(Follower.id == api_row.id))
    res = await db.execute(
        update(Follower)
        .where(Follower.id == browser_row.id, Follower.ig_user_id < 0)
        .values(ig_user_id=pk_vero, updated_at=datetime.utcnow())
    )
    if res.rowcount != 1:
        await db.rollback()
        return ("la riga da promuovere non e' piu' provvisoria (o non esiste): "
                "coppia lasciata intatta")
    await db.commit()
    return "fusa"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="scrive davvero (default: dry-run)")
    ap.add_argument("--campaign", default=None, help="limita a una campagna")
    ap.add_argument("--backup", default=None, help="percorso del backup JSON")
    ap.add_argument("--tutte-le-campagne", action="store_true",
                    help="senza --campaign, conferma di voler leggere TUTTE le campagne")
    args = ap.parse_args()

    # Su quale database si sta per lavorare: AsyncSessionLocal segue il .env
    # risolto dalla working directory, e in un worktree e' precisamente il modo
    # per colpire il DB sbagliato senza accorgersene.
    print(f"\nDatabase: {_db_mascherato()}")
    if not args.campaign and not args.tutte_le_campagne:
        print("Specifica --campaign <id>, oppure --tutte-le-campagne se e' voluto.")
        return

    async with AsyncSessionLocal() as db:
        coppie, scartate = await _coppie(db, args.campaign)

        fondibili, bloccate = [], []
        for cid, u, browser_row, api_row in coppie:
            motivo = await _api_e_una_scheda_vuota(db, api_row)
            (bloccate if motivo else fondibili).append((cid, u, browser_row, api_row, motivo))

        print(f"\n{'APPLICO' if args.apply else 'DRY-RUN'} — "
              f"{len(fondibili)} coppie fondibili, {len(bloccate)} bloccate, "
              f"{len(scartate)} scartate\n")
        for cid, u, b, a, _ in fondibili:
            print(f"  @{u:35} tengo {b.id[:8]} (targa {b.ig_user_id} -> {a.ig_user_id}), "
                  f"cancello {a.id[:8]}")
        for cid, u, b, a, motivo in bloccate:
            print(f"  [SALTATA] @{u}: {motivo}")
        for cid, u, motivo in scartate:
            print(f"  [SCARTATA] @{u}: {motivo}")

        if not args.apply:
            print("\nNiente scritto (dry-run). Aggiungi --apply per eseguire.")
            return
        if not fondibili:
            print("\nNiente da fare.")
            return

        percorso = args.backup or os.path.join(
            "backups", f"bonifica_doppioni_{datetime.utcnow():%Y%m%d_%H%M%S}.json"
        )
        os.makedirs(os.path.dirname(percorso) or ".", exist_ok=True)
        with open(percorso, "w", encoding="utf-8") as fh:
            json.dump(
                [{"campaign_id": cid, "username": u,
                  "riga_browser": _riga_dict(b), "riga_api": _riga_dict(a)}
                 for cid, u, b, a, _ in fondibili],
                fh, ensure_ascii=False, indent=2,
            )
        print(f"\nBackup delle {len(fondibili)*2} righe: {percorso}")

        fatte, errori = 0, []
        campagne_toccate = set()
        for cid, u, browser_row, api_row, _ in fondibili:
            try:
                esito = await fondi_coppia(db, browser_row, api_row)
            except Exception as e:      # noqa: BLE001 — una coppia rotta non ferma le altre
                await db.rollback()
                errori.append((u, repr(e)))
                continue
            if esito == "fusa":
                fatte += 1
                campagne_toccate.add(cid)
            else:
                errori.append((u, esito))

        for cid in campagne_toccate:
            n = await db.scalar(
                select(func.count(Follower.id)).where(Follower.campaign_id == cid)
            ) or 0
            campaign = await db.get(Campaign, cid)
            if campaign is not None:
                campaign.total_followers = n
                campaign.updated_at = datetime.utcnow()
        await db.commit()

        print(f"\nFuse {fatte} coppie. Contatore riallineato su {len(campagne_toccate)} campagne.")
        for u, err in errori:
            print(f"  [ERRORE] @{u}: {err}")


if __name__ == "__main__":
    asyncio.run(main())
