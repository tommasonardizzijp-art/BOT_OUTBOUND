"""Salvataggio dei contatti raccolti dal browser: dedup per USERNAME.

Perche' non per targa: i contatti raccolti via API hanno full_name=None
(scrape_inbox.py:179), quindi non sono riconoscibili dal nome e le loro chat
vengono riaperte. Arrivano con una targa provvisoria diversa dalla targa vera che
hanno gia' in archivio: un dedup sulla targa non scatterebbe e creerebbe una riga
duplicata per OGNI contatto gia' presente. Su una campagna con arricchimento
attivo, quella riga duplicata puo' portare a un secondo DM alla stessa persona.

Questo NON sostituisce UniqueConstraint(campaign_id, ig_user_id), che resta a
proteggere il percorso API: sono due reti a maglie diverse.

La lookup e' ESPLICITA, non si tenta l'INSERT lasciando parlare il vincolo:
sui due percorsi della Fase Bio quell'eccezione e' gestita in modi diversi, e in
uno dei due blocca il batch per sempre (browser_bio.py:1362 fa break senza
marcare il follower, e la selezione e' limit(1) senza ORDER BY: il giro dopo
ripesca la stessa riga).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy import select

from app.models.follower import Follower, FollowerStatus
from app.services.inbox_browser.targa import (
    e_provvisoria, normalizza_username, targa_provvisoria,
)

# Ordine di avanzamento: piu' avanti = vince in una fusione.
# replied/failed/skipped sono tutti stati terminali del ciclo di invio: nessuno
# dei tre deve tornare indietro verso pending_approval/sent, ma non c'e' un
# ordine sensato TRA loro, quindi condividono lo stesso rango massimo.
_ORDINE = {
    FollowerStatus.pending: 0,
    FollowerStatus.bio_scraped: 1,
    FollowerStatus.message_generated: 2,
    FollowerStatus.pending_approval: 3,
    FollowerStatus.sent: 4,
    FollowerStatus.replied: 5,
    FollowerStatus.failed: 5,
    FollowerStatus.skipped: 5,
}


def stato_vincente(a: FollowerStatus, b: FollowerStatus) -> FollowerStatus:
    """In una fusione lo stato piu' avanzato vince SEMPRE.

    Un contatto gia' `sent` che tornasse `pending` riceverebbe un secondo DM.
    """
    return a if _ORDINE.get(a, 0) >= _ORDINE.get(b, 0) else b


@dataclass
class DatiContatto:
    username: str
    nome: str | None
    last_message_at: datetime | None
    last_message_from: str | None   # 'us' | 'them' | None
    last_message_text: str | None


async def salva_contatto(db, campaign_id: str, dati: DatiContatto) -> str:
    """Crea o aggiorna il contatto. Ritorna 'creato' o 'aggiornato'."""
    username = normalizza_username(dati.username)
    if not username:
        raise ValueError("username vuoto: il contatto non e' identificabile")

    esistente = (await db.execute(
        select(Follower).where(
            Follower.campaign_id == campaign_id,
            Follower.username == username,
        )
    )).scalar_one_or_none()

    if esistente is None:
        db.add(Follower(
            campaign_id=campaign_id,
            ig_user_id=targa_provvisoria(username),
            username=username,
            full_name=dati.nome,
            is_private=False,
            is_verified=False,
            status=FollowerStatus.pending,
            last_message_at=dati.last_message_at,
            last_message_from=dati.last_message_from,
            last_message_text=dati.last_message_text,
            source_channel="browser",
        ))
        await db.commit()
        return "creato"

    # Fusione: si integra, non si sovrascrive.
    if not esistente.full_name and dati.nome:
        esistente.full_name = dati.nome
    esistente.last_message_at = dati.last_message_at or esistente.last_message_at
    esistente.last_message_from = dati.last_message_from or esistente.last_message_from
    esistente.last_message_text = dati.last_message_text or esistente.last_message_text
    esistente.status = stato_vincente(esistente.status, FollowerStatus.pending)
    esistente.updated_at = datetime.utcnow()

    # La targa VERA non si tocca mai: sostituirla con una provvisoria
    # sgancerebbe il contatto da GlobalContact e dalle prenotazioni.
    if e_provvisoria(esistente.ig_user_id):
        atteso = targa_provvisoria(username)
        if esistente.ig_user_id != atteso:
            logger.info(
                f"[InboxBrowser] @{username}: targa provvisoria riallineata "
                f"({esistente.ig_user_id} -> {atteso})"
            )
            esistente.ig_user_id = atteso

    await db.commit()
    return "aggiornato"
