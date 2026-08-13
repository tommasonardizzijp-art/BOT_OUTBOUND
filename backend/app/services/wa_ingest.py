"""Ingest CSV -> contatti WhatsApp (SDD 7.1).

Tre regole, tutte con un test dedicato:
  1. un numero non normalizzabile in modo NON ambiguo si scarta con un
     motivo, non si aggiusta;
  2. un contatto opted_out/do_not_contact e' escluso e RIPORTATO -- mai
     re-incluso da un file nuovo (l'opt-out vince sull'ingest);
  3. il numero in chiaro non esce mai da questa funzione: ne' nei log, ne'
     nel report, ne' nei messaggi d'errore.

Riga per riga e idempotente (Q21): un import interrotto a meta' si sana
ricaricando lo stesso file.

Nota sulla forma dell'"e164" salvata (deviazione dal testo del piano):
`normalize_e164` di M1 ritorna le cifre SENZA '+' (e' il suo contratto
esplicito, vedi docstring e test di phone_pseudonym.py: 'a 393421460077
senza +'). Le factory condivise (tests/factories_wa.py, patrimonio comune
M2/M3) e i test di questo modulo usano invece la forma CON '+' per
hmac_phone()/encrypt(): e' quella forma, e non l'output nudo di
normalize_e164, che deve restare la chiave (phone_hmac) e il valore
cifrato di WaContact -- altrimenti un contatto creato da ingest e uno
creato dalle factory per lo STESSO numero finirebbero con due phone_hmac
diversi, e la dedup/DNC smetterebbe di funzionare in modo silenzioso.
Qui si normalizza con la funzione di M1 (che non si tocca) e si
ricompone il '+' subito dopo, prima di ogni hmac_phone()/encrypt().
"""
import json
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.services.wa_csv import COLONNA_NOME, COLONNA_NUMERO, parse_wa_csv
from app.utils.crypto import encrypt
from app.utils.phone_pseudonym import (PhoneNormalizationError, hmac_e164,
                                       normalize_e164)


@dataclass
class Scarto:
    riga: int
    motivo: str
    valore_mascherato: str


@dataclass
class ReportIngest:
    creati: int = 0
    aggiornati: int = 0
    gia_dnc: int = 0
    duplicati_nel_file: int = 0
    scarti: list[Scarto] = field(default_factory=list)


def _maschera_grezzo(valore: str) -> str:
    """Un numero malformato spesso NON e' normalizzabile, quindi non esiste
    una forma E.164 su cui applicare mask_phone: si maschera a mano il
    valore grezzo, primi 3 e ultimi 2 caratteri. Il report va all'admin, ma
    resta un documento con dentro dati personali."""
    v = (valore or "").strip()
    if len(v) <= 5:
        return "•" * len(v)
    return f"{v[:3]}{'•' * max(3, len(v) - 5)}{v[-2:]}"


def _attributi(valori: dict, colonne_attributo: list[str]) -> dict | None:
    attrs = {k: valori.get(k, "") for k in colonne_attributo if valori.get(k, "")}
    if not attrs:
        return None
    # Tetto agli attributi (Q15): si tronca il singolo valore invece di
    # scartare il contatto -- il testo lungo e' un problema di chi ha
    # esportato il CSV, non un motivo per perdere un cliente.
    limite = int(settings.wa_ingest_max_attrs_bytes)
    while len(json.dumps(attrs)) > limite and attrs:
        piu_lungo = max(attrs, key=lambda k: len(str(attrs[k])))
        if len(str(attrs[piu_lungo])) <= 8:
            attrs.pop(piu_lungo)
        else:
            attrs[piu_lungo] = str(attrs[piu_lungo])[: max(8, len(str(attrs[piu_lungo])) // 2)]
    return attrs or None


def _motivo_pulito(exc: PhoneNormalizationError) -> str:
    """Il messaggio dell'eccezione contiene il numero in chiaro: si tiene
    solo la PARTE DIAGNOSTICA, prima dei due punti. Verificato a mano
    (2026-07-30) sui tre `raise` di phone_pseudonym.normalize_e164: ognuno
    ha la forma "<diagnosi>: {raw!r}", quindi il raw sta SEMPRE dopo il
    primo ':' e non prima. Se un domani un `raise` in phone_pseudonym.py
    (patrimonio M1, non si tocca) cambiasse questa forma, questa funzione
    andrebbe sostituita con una mappa esplicita tipo-errore -> motivo,
    NON aggiustata qui."""
    testo = str(exc)
    return testo.split(":")[0].strip() if ":" in testo else type(exc).__name__


async def ingerisci_csv(db, *, tenant_id: str, campaign_id: str,
                        contenuto: bytes) -> ReportIngest:
    from app.models.wa import WaCampaign, WaCampaignContact, WaContact, WaContactStatus

    righe, colonne_attributo = parse_wa_csv(contenuto)
    report = ReportIngest()
    visti: set[str] = set()
    adesso = datetime.utcnow()

    for riga in righe:
        grezzo = riga.valori.get(COLONNA_NUMERO, "")
        try:
            numero = normalize_e164(grezzo, default_country=settings.wa_ingest_default_country)
        except PhoneNormalizationError as exc:
            # MAI str(exc): contiene il numero in chiaro (contratto §2.3).
            motivo = _motivo_pulito(exc)
            report.scarti.append(Scarto(riga.numero_riga, motivo, _maschera_grezzo(grezzo)))
            logger.info(f"[WA ingest] riga {riga.numero_riga} scartata: {motivo}")
            continue

        # normalize_e164 ritorna le cifre SENZA '+' (contratto M1): si
        # ricompone qui per encrypt() (che vuole la forma leggibile CON '+',
        # vedi docstring del modulo), mentre hmac_e164 ricompone da se' per
        # la pseudonimizzazione -- stessa funzione condivisa usata da
        # wa_discover/salvataggio.py (AVVIO 12/08 §1, passo 3).
        e164 = "+" + numero

        pseudo = hmac_e164(numero)
        if pseudo in visti:
            report.duplicati_nel_file += 1
            continue
        visti.add(pseudo)

        contatto = await db.scalar(
            select(WaContact).where(WaContact.tenant_id == tenant_id,
                                    WaContact.phone_hmac == pseudo))
        attrs = _attributi(riga.valori, colonne_attributo)
        nome = riga.valori.get(COLONNA_NOME, "") or None

        if contatto is None:
            contatto = WaContact(tenant_id=tenant_id, phone_hmac=pseudo,
                                 encrypted_phone=encrypt(e164), display_name=nome,
                                 attributes=attrs, first_seen_at=adesso)
            try:
                # SAVEPOINT: due ingest concorrenti sullo stesso numero (due
                # richieste, due campagne diverse dello stesso tenant, o un
                # doppio submit) possono passare ENTRAMBE la SELECT qui
                # sopra prima che l'altra abbia fatto INSERT. Trovato in
                # Fase 4 QA (adversarial #20/#24): senza la savepoint, la
                # UNIQUE(tenant_id, phone_hmac) sollevava un IntegrityError
                # non catturato -> 500 grezzo, e nel caso fra due campagne
                # diverse l'intero batch della richiesta perdente veniva
                # perso in silenzio (rollback dell'intera transazione).
                async with db.begin_nested():
                    db.add(contatto)
                    await db.flush()
                report.creati += 1
            except IntegrityError:
                # L'altra richiesta ha vinto la corsa: il contatto esiste
                # gia', si rilegge e si procede come nel ramo "trovato"
                # sotto -- stesso comportamento, solo scoperto piu' tardi.
                contatto = await db.scalar(
                    select(WaContact).where(WaContact.tenant_id == tenant_id,
                                            WaContact.phone_hmac == pseudo))
                if nome:
                    contatto.display_name = nome
                if attrs:
                    contatto.attributes = {**(contatto.attributes or {}), **attrs}
                report.aggiornati += 1
        else:
            # Gap-fill (Q16): si aggiorna cio' che il file porta, non si
            # cancella cio' che c'era.
            if nome:
                contatto.display_name = nome
            if attrs:
                contatto.attributes = {**(contatto.attributes or {}), **attrs}
            report.aggiornati += 1

        if contatto.opted_out or contatto.do_not_contact:
            # L'opt-out vince sull'ingest, sempre e comunque (SDD 7.5.5).
            report.gia_dnc += 1
            continue

        esistente = await db.scalar(
            select(WaCampaignContact).where(
                WaCampaignContact.campaign_id == campaign_id,
                WaCampaignContact.contact_id == contatto.id))
        if esistente is None:
            db.add(WaCampaignContact(
                campaign_id=campaign_id, contact_id=contatto.id,
                status=WaContactStatus.queued, current_step=-1,
                # Contratto §7.2: MAI NULL su una riga non terminale (I3).
                next_action_at=adesso, failure_count=0,
            ))

    await db.flush()
    # Contatore denormalizzato: e' di M2 (contratto §4.1).
    campagna = await db.scalar(select(WaCampaign).where(WaCampaign.id == campaign_id))
    if campagna is not None:
        campagna.total_contacts = await db.scalar(
            select(func.count(WaCampaignContact.id))
            .where(WaCampaignContact.campaign_id == campaign_id)) or 0
    await db.commit()

    logger.info(f"[WA ingest] campagna={campaign_id} creati={report.creati} "
                f"aggiornati={report.aggiornati} dnc={report.gia_dnc} "
                f"dup={report.duplicati_nel_file} scarti={len(report.scarti)}")
    return report
