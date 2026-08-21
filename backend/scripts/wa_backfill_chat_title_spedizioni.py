"""Backfill 21/08: recupero dei contatti corrotti da _impara_chat_title
(bug fisso in wa_sender.py, vedi project_state.md sezione 21/08). 283
contatti di una campagna sola (PRIMERO MAGAZZINO, numero e4b020cc) hanno
chat_title='SPEDIZIONI' -- il titolo di un'altra chat che scavalcava la
loro nella sidebar nella finestra fra invio e apprendimento. Effetto: il
reply-watcher non ha mai potuto riagganciare nessuno di loro a uno STOP o
una risposta vera dal 18/08 a oggi (matched_by sempre 'none').

Decisione di Tommaso (21/08): recuperarli tutti, STOP e risposte, aprendo
le chat una volta -- consapevole che questo le marca "lette" su WhatsApp.

Per ogni contatto:
  1. apre la chat (via ricerca, MAI deep-link) -- registra se era "da
     leggere" PRIMA del click (OpenResult.era_non_letto): l'unico dato che
     serve per dare a Tommaso la lista di chi ripristinare a mano (nessun
     selettore per "segna come da leggere" mai verificato dal vivo, non lo
     si indovina qui su chat vere);
  2. legge il titolo vero dall'header (read_open_chat_title) e lo
     sovrascrive -- da solo riabilita il matching futuro del reply-watcher;
  3. carica la cronologia e cerca uno STOP nella coda intera (nessuna
     scadenza, read_inbound_tail) -- se trovato, persiste l'opt-out con lo
     STESSO giudizio del reply-watcher (import diretto delle sue funzioni,
     logica non duplicata) e si ferma li' (uno STOP non e' anche 'ha
     risposto'). Altrimenti cerca una risposta GENUINA entro 48h dal nostro
     invio (read_inbound_since, timestamp reale via data-pre-plain-text --
     fix 21/08: read_inbound_tail da solo non ha nozione di tempo e
     produceva falsi 'ha risposto' su corrispondenza organica precedente o
     sull'avviso di crittografia, misurato 3/4 nel primo pilota).

SOLA LETTURA per il composer: nessun send_text, mai, in nessun ramo.

Uso:
    python scripts/wa_backfill_chat_title_spedizioni.py --limit 5 [--dry-run]
    python scripts/wa_backfill_chat_title_spedizioni.py            # tutti

--dry-run: apre comunque le chat reali (non esiste un dry-run che non
tocchi WhatsApp, aprire E' l'operazione) ma non scrive nulla a DB e non
persiste opt-out/replied -- serve solo a vedere il report prima di
impegnarsi.
"""
import argparse
import asyncio
import random
import unicodedata
from datetime import timedelta

from loguru import logger
from sqlalchemy import select

from app.browser.whatsapp_page import WhatsAppWebPage, title_is_number
from app.database import AsyncSessionLocal
from app.models.wa import WaContact, WaContactStatus, WaMessage, WaMessageStatus
from app.services import wa_optout, wa_profile_lock
from app.services.wa_reply_watcher import (_campagna_attiva_del_contatto,
                                            _incrementa_contatore_campagna,
                                            _riga_da_marcare_replied)
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise
from app.utils import events
from app.utils.crypto import decrypt
from app.utils.phone_pseudonym import mask_phone
from app.utils.tempo import adesso_utc

NUMERO_ID = "e4b020cc-f906-4dbe-981a-27a4973c253f"
TITOLO_CORROTTO = "SPEDIZIONI"


async def _contatti_da_recuperare(limit: int | None) -> list[str]:
    async with AsyncSessionLocal() as db:
        q = select(WaContact.id).where(WaContact.chat_title == TITOLO_CORROTTO) \
                                 .order_by(WaContact.first_seen_at)
        if limit:
            q = q.limit(limit)
        return list((await db.execute(q)).scalars().all())


async def _recupera_un_contatto(pom, contact_id: str, *, dry_run: bool) -> dict:
    esito = {"contact_id": contact_id, "masked": None, "era_non_letto": None,
             "titolo_recuperato": None, "titolo_era_numero": False,
             "trovato_stop": False, "trovato_replied": False, "errore": None}

    async with AsyncSessionLocal() as db:
        contact = await db.get(WaContact, contact_id)
        if contact is None:
            esito["errore"] = "contatto_sparito"
            return esito
        e164 = decrypt(contact.encrypted_phone)
        esito["masked"] = mask_phone(e164)

        try:
            aperto = await pom.open_chat(e164)
        except Exception as exc:
            esito["errore"] = f"open_chat: {type(exc).__name__}"
            return esito
        esito["era_non_letto"] = aperto.era_non_letto
        if not aperto.ok:
            esito["errore"] = f"non_aperta:{aperto.signal}"
            return esito

        # --- 2. titolo vero dall'header -------------------------------------
        # Sempre corretto a DB, non solo quando si impara un nome: se il
        # titolo vero e' un numero puro (contatto non in rubrica, P12 vieta
        # di salvarlo) lasciare 'SPEDIZIONI' li' dentro sarebbe comunque
        # sbagliato -- non fa danno OGGI (match_contact prende la via
        # phone_hmac quando title_is_number, non guarda chat_title), ma
        # blocca per sempre un apprendimento futuro corretto (se il cliente
        # viene salvato in rubrica piu' avanti, chat_title gia' valorizzato
        # impedisce a _impara_chat_title di re-imparare, e' guardia sul
        # primo invio soltanto). Azzerarlo qui lo riapre.
        try:
            titolo = await pom.read_open_chat_title()
        except Exception as exc:
            titolo = None
            logger.debug(f"[BACKFILL] {esito['masked']}: titolo non letto "
                         f"({type(exc).__name__})")
        if titolo and not title_is_number(titolo):
            titolo_nfc = unicodedata.normalize("NFC", titolo)[:200]
            esito["titolo_recuperato"] = titolo_nfc
            if not dry_run:
                contact.chat_title = titolo_nfc
                await db.commit()
        elif titolo and title_is_number(titolo):
            esito["titolo_era_numero"] = True
            if not dry_run and contact.chat_title == TITOLO_CORROTTO:
                contact.chat_title = None
                await db.commit()

        # --- 3a. STOP: nessuna scadenza, si legge SEMPRE tutta la coda ------
        # (SDD 6.4/7.5: un opt-out vale in qualunque momento arrivi, non solo
        # nei giorni subito dopo la campagna -- read_inbound_tail resta lo
        # stesso metodo della guardia pre-invio, invariato).
        await pom.load_history(minimo=300)
        coda = await pom.read_inbound_tail(n=100)
        if coda is None:
            esito["errore"] = (esito["errore"] or "") + ";cecita_coda"
            return esito

        stop_trovato = next((t for t in coda if wa_optout.looks_like_stop(t)), None)
        if stop_trovato:
            esito["trovato_stop"] = True
            if not dry_run:
                gia_optato = bool(contact.opted_out)
                cc_attiva = await _campagna_attiva_del_contatto(db, contact.id)
                await wa_optout.persist_wa_optout(
                    db, contact.id, prova=stop_trovato,
                    campaign_id=cc_attiva.campaign_id if cc_attiva else None)
                if cc_attiva is not None and not gia_optato:
                    await _incrementa_contatore_campagna(db, cc_attiva.campaign_id, "opted_out")
                    await wa_optout.check_optout_circuit_breaker(db, cc_attiva.campaign_id)
            return esito  # STOP e replied si escludono: uno STOP non e' anche 'ha risposto'

        # --- 3b. replied: SOLO risposte genuine entro 48h dal nostro invio -
        # Bug reale 21/08: read_inbound_tail (usato qui nella prima versione
        # dello script) non ha nozione di tempo -- su chat con
        # corrispondenza organica precedente (il numero e' condiviso con
        # l'assistenza clienti umana) o con l'avviso di crittografia (nessun
        # mittente, classify_direction lo conta 'in' per sicurezza sullo
        # STOP) produceva un falso 'ha risposto'. Misurato: 3 falsi positivi
        # su 4 nel pilota. read_inbound_since legge il timestamp REALE
        # (data-pre-plain-text) e scarta tutto cio' che non e' strettamente
        # dopo il nostro invio ed entro 48h.
        msg = await db.scalar(
            select(WaMessage).where(WaMessage.contact_id == contact.id,
                                    WaMessage.status == WaMessageStatus.sent)
            .order_by(WaMessage.sent_at.desc()))
        if msg is None or msg.sent_at is None:
            esito["errore"] = (esito["errore"] or "") + ";nessun_invio_confermato_a_db"
            return esito

        entro = msg.sent_at + timedelta(hours=48)
        risposte = await pom.read_inbound_since(msg.sent_at, entro=entro, n=20)
        if risposte is None:
            esito["errore"] = (esito["errore"] or "") + ";cecita_timestamp"
            return esito

        if risposte:
            esito["trovato_replied"] = True
            if not dry_run:
                cc = await _riga_da_marcare_replied(db, contact.id)
                if cc is not None:
                    cc.status = WaContactStatus.replied
                    cc.replied_at_step = cc.current_step
                    cc.next_action_at = None
                    contact.last_replied_at = adesso_utc()
                    await _incrementa_contatore_campagna(db, cc.campaign_id, "replied")
                    await db.commit()
                    events.emit(cc.campaign_id, "wa.reply.received",
                                f"contatto {contact.id[:8]}: risposta recuperata "
                                "da backfill 21/08", level="info")

    return esito


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    contatti = await _contatti_da_recuperare(args.limit)
    logger.info(f"[BACKFILL] {len(contatti)} contatti da processare "
               f"(dry_run={args.dry_run})")
    if not contatti:
        return

    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, NUMERO_ID)
        proxy_url = numero.proxy_url

    risultati = []
    async with wa_profile_lock.held(NUMERO_ID):
        async with _open_wa_browser(NUMERO_ID, headless=True, proxy_url=proxy_url) as context:
            page = await context.new_page()
            await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
            pom = WhatsAppWebPage(page)
            stato = await pom.session_state()
            if stato != "logged_in":
                logger.error(f"[BACKFILL] sessione non pronta: {stato}")
                return

            for i, contact_id in enumerate(contatti):
                esito = await _recupera_un_contatto(pom, contact_id, dry_run=args.dry_run)
                risultati.append(esito)
                logger.info(f"[BACKFILL] [{i+1}/{len(contatti)}] {esito}")
                await asyncio.sleep(random.uniform(3.0, 7.0))

    non_letti = [r["masked"] for r in risultati if r["era_non_letto"]]
    stop_trovati = [r for r in risultati if r["trovato_stop"]]
    replied_trovati = [r for r in risultati if r["trovato_replied"]]
    errori = [r for r in risultati if r["errore"]]

    print("\n=== REPORT BACKFILL ===")
    print(f"processati: {len(risultati)}")
    print(f"titolo recuperato (nome vero): {sum(1 for r in risultati if r['titolo_recuperato'])}")
    print(f"titolo era un numero (chat_title azzerato, phone_hmac gia' sufficiente): "
         f"{sum(1 for r in risultati if r['titolo_era_numero'])}")
    print(f"STOP trovati: {len(stop_trovati)}")
    print(f"replied trovati: {len(replied_trovati)}")
    print(f"errori: {len(errori)}")
    print(f"\nera' da leggere PRIMA dell'apertura ({len(non_letti)}):")
    for m in non_letti:
        print(f"  {m}")
    if errori:
        print("\nerrori dettaglio:")
        for r in errori:
            print(f"  {r}")


if __name__ == "__main__":
    asyncio.run(main())
