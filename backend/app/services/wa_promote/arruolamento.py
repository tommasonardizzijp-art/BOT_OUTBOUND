"""Fase B, Task 3: `WaContact` esistenti -> `WaCampaignContact` di una
campagna (arruolamento).

Stesso schema della seconda meta' di `wa_ingest.ingerisci_csv` (righe
176-199): qui il contatto ESISTE gia' (niente CSV, niente
normalizzazione/hash) -- serve anche fuori dal flusso Fase B (aggiungere
contatti gia' noti a una campagna esistente), quindi vive in un proprio file
anche se non e' "promozione" in senso stretto; sta comunque in `wa_promote/`
perche' e' la Fase B a introdurlo per prima.

Persistenza con lookup esplicita + SAVEPOINT/IntegrityError come ripiego
sulla concorrenza (vincolo globale del piano Fase B, stesso schema di
`wa_ingest`/`promozione.py`): due chiamate `arruola()` concorrenti sullo
stesso `(campaign_id, contact_id)` -- lo stesso contatto aggiunto due volte
in fretta, o un doppio submit -- possono passare ENTRAMBE la SELECT di
`esistente` prima che l'altra faccia INSERT; senza la savepoint la
UNIQUE(campaign_id, contact_id) solleverebbe un IntegrityError non
catturato.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.wa import (WaCampaign, WaCampaignContact, WaCampaignStatus,
                           WaContact, WaContactStatus)
from app.utils.ids import uuid_valido
from app.utils.tempo import adesso_utc


class CampagnaNonModificabile(Exception):
    """Sollevata da `arruola()` quando la campagna richiesta non puo'
    ricevere nuovi contatti: non esiste, oppure esiste ma non e' in bozza
    (status != draft).

    Un solo tipo per entrambi i casi -- il piano specifica esplicitamente
    solo il secondo ("solleva CampagnaNonModificabile se campagna.status !=
    draft"), ma una campagna inesistente non e' modificabile a maggior
    ragione, e senza un guard esplicito qui l'accesso a `campagna.status` su
    un `None` sarebbe un `AttributeError` -- un 500 grezzo invece di un
    errore chiaro che l'API (Task 4) puo' tradurre in un codice HTTP
    sensato. Definita qui e non in un modulo condiviso, stesso stile di
    `CsvParseError` in `wa_csv.py` e `WaProfileBusy` in
    `wa_profile_lock.py`: ogni eccezione di dominio vive dove nasce, finche'
    non serve altrove."""


@dataclass
class Scarto:
    id: str
    motivo: str


@dataclass
class ReportArruolamento:
    arruolati: int = 0
    gia_presenti: int = 0
    gia_dnc: int = 0
    scarti: list[Scarto] = field(default_factory=list)


async def arruola(db, *, campaign_id: str, contact_ids: list[str]) -> ReportArruolamento:
    """Aggiunge `contact_ids` (WaContact gia' esistenti) alla campagna
    `campaign_id`, se e' in bozza.

    La campagna si carica e si verifica UNA volta a inizio funzione
    (fail-fast, prima di processare qualunque contact_id): e' una sola per
    l'intera chiamata, non per singolo id -- verificarla ad ogni iterazione
    sarebbe lavoro ripetuto per una condizione che non cambia durante il
    batch, e un batch parzialmente scritto prima di scoprire lo stato sbagliato
    sarebbe l'opposto di "fail-fast".

    Per ogni contact_id, nell'ordine (stesso ordine del piano):
      1. non trovato, o di un tenant diverso da quello della campagna --
         stesso principio "per costruzione" del gap IDOR gia' corretto in
         `promozione.promuovi`: mai un motivo diverso da
         'contatto_inesistente', altrimenti chi indovina un id di un altro
         tenant distinguerebbe dall'esterno "non esiste" da "non e' tuo" ->
         Scarto.
      2. opted_out/do_not_contact -> gia_dnc. L'opt-out vince sempre
         (vincolo globale del piano): anche se il contatto fosse gia'
         arruolato da prima e poi opted-out nel frattempo, qui si conta
         come gia_dnc, non si tocca la riga esistente (M3 la vedra' comunque
         come opted_out sul WaContact al momento dell'invio).
      3. gia' arruolato in questa campagna -> gia_presenti (idempotente).
      4. altrimenti crea con status=queued, current_step=-1,
         next_action_at=adesso (contratto §7.2, I3: mai NULL su una riga non
         terminale), failure_count=0.
    """
    campagna = await db.get(WaCampaign, campaign_id) if uuid_valido(campaign_id) else None
    if campagna is None or campagna.status != WaCampaignStatus.draft:
        motivo = "inesistente" if campagna is None else f"stato {campagna.status.value}"
        raise CampagnaNonModificabile(f"campagna {campaign_id} non modificabile: {motivo}")

    report = ReportArruolamento()
    adesso = adesso_utc()

    for contact_id in contact_ids:
        contatto = await db.get(WaContact, contact_id) if uuid_valido(contact_id) else None
        if contatto is None or contatto.tenant_id != campagna.tenant_id:
            report.scarti.append(Scarto(contact_id, "contatto_inesistente"))
            continue

        if contatto.opted_out or contatto.do_not_contact:
            report.gia_dnc += 1
            continue

        esistente = await db.scalar(
            select(WaCampaignContact).where(
                WaCampaignContact.campaign_id == campaign_id,
                WaCampaignContact.contact_id == contact_id))
        if esistente is not None:
            report.gia_presenti += 1
            continue

        try:
            # SAVEPOINT: vedi docstring del modulo -- due arruolamenti
            # concorrenti sullo stesso (campaign_id, contact_id) possono
            # passare entrambi la SELECT `esistente` sopra prima che l'altro
            # abbia fatto INSERT.
            async with db.begin_nested():
                db.add(WaCampaignContact(
                    campaign_id=campaign_id, contact_id=contact_id,
                    status=WaContactStatus.queued, current_step=-1,
                    next_action_at=adesso, failure_count=0,
                ))
                await db.flush()
            report.arruolati += 1
        except IntegrityError:
            # L'altra chiamata concorrente ha vinto la corsa: la riga esiste
            # gia', si conta come 'gia_presenti', non come errore.
            report.gia_presenti += 1

    if report.arruolati:
        # Mai un read-modify-write (stesso vincolo di
        # wa_contacts.rimuovi_contatto:137-139, qui in incremento invece che
        # in decremento): un arruolamento concorrente su un'altra chiamata
        # non deve perdere un incremento.
        await db.execute(
            update(WaCampaign).where(WaCampaign.id == campaign_id)
            .values(total_contacts=WaCampaign.total_contacts + report.arruolati))

    await db.flush()
    await db.commit()

    logger.info(f"[WA arruolamento] campagna={campaign_id} "
               f"arruolati={report.arruolati} gia_presenti={report.gia_presenti} "
               f"gia_dnc={report.gia_dnc} scarti={len(report.scarti)}")
    return report
