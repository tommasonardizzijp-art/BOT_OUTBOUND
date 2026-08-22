# backend/app/services/dm_harvest.py
"""Persistenza dei dati colti passivamente durante la visita che il DM fa comunque.

Regole non negoziabili (vedi spec 2026-08-07, S4.2):
  - gira DOPO che il follower e' stato marcato 'sent': un guasto qui non deve
    mai alterare la contabilita' dell'invio;
  - non solleva MAI;
  - non sovrascrive campi gia' valorizzati da una fase dedicata.
"""
from datetime import datetime

from loguru import logger
from sqlalchemy.exc import IntegrityError

# Campi che l'harvest puo' riempire, con il nome corrispondente sullo shim.
_CAMPI = (
    "full_name", "biography", "follower_count", "following_count", "external_url",
)


async def harvest_profile_into_follower(db, follower, payload: dict | None) -> bool:
    """Riempie i campi VUOTI del follower col payload passivo. True se ha scritto."""
    if not payload:
        return False
    # Catturato PRIMA del try: se il commit fallisce la sessione entra in stato
    # 'pending rollback' e rileggere un attributo ORM del follower (es. .username,
    # per il log stesso) puo' far risalire PendingRollbackError -- esattamente
    # l'eccezione che questa funzione promette di non far mai risalire.
    username = getattr(follower, "username", "?")
    try:
        from app.services.browser_bio import graphql_user_to_web_shape, web_user_to_shim

        shim = web_user_to_shim(graphql_user_to_web_shape(payload))

        # Ancoraggio della targa. La visita l'abbiamo gia' pagata per mandare il DM e
        # il pk e' dentro il payload catturato: prima di questo blocco veniva buttato,
        # e una targa provvisoria restava provvisoria per sempre.
        from app.services.browser_bio import decidi_sostituzione_targa

        esito_targa = decidi_sostituzione_targa(follower.ig_user_id, getattr(shim, "pk", None))

        if esito_targa == "identita_cambiata":
            # Lo username ha cambiato proprietario: il profilo appena visitato e'
            # di un'altra persona. Il DM e' gia' partito (scelta esplicita: si
            # accetta il caso raro invece di bloccare gli invii), ma i dati dello
            # sconosciuto NON vanno sulla scheda del contatto del cliente: e' la
            # scheda che poi usa lui, e un dato sbagliato li' non si nota mai piu'.
            logger.error(
                f"[Harvest] @{username}: pk diverso da quello registrato "
                f"({follower.ig_user_id} -> {shim.pk}). Handle riassegnato: "
                "non scrivo nulla, il contatto va ri-arricchito prima di ricontattarlo."
            )
            follower.skip_reason = "handle_riassegnato"
            follower.updated_at = datetime.utcnow()
            await db.commit()
            return False

        if esito_targa == "sostituisci":
            # UniqueConstraint(campaign_id, ig_user_id): se un'altra riga della
            # stessa campagna porta gia' il pk vero, scrivere qui solleverebbe.
            # Stessa scelta di browser_bio.py:570-598 — skip e segnalazione, mai
            # un merge indovinato.
            from sqlalchemy import select
            from app.models.follower import Follower

            bersaglio = (await db.execute(
                select(Follower).where(
                    Follower.campaign_id == follower.campaign_id,
                    Follower.ig_user_id == int(shim.pk),
                    Follower.id != follower.id,
                )
            )).scalar_one_or_none()
            if bersaglio is not None:
                logger.error(
                    f"[Harvest] @{username}: la targa vera {shim.pk} e' gia' su "
                    "un'altra riga della campagna. Non fondo automaticamente: "
                    "segnalo e lascio."
                )
                follower.skip_reason = "targa_gia_presente_su_altra_riga"
                follower.updated_at = datetime.utcnow()
                await db.commit()
                return False
            follower.ig_user_id = int(shim.pk)
            scritto_targa = True
        else:
            scritto_targa = False

        scritto = scritto_targa
        for campo in _CAMPI:
            nuovo = getattr(shim, campo, None)
            if nuovo in (None, ""):
                continue
            if getattr(follower, campo, None) in (None, ""):
                setattr(follower, campo, nuovo)
                scritto = True

        # I booleani hanno sempre un valore: si aggiornano solo se il payload
        # e' esplicito, e non contano come "campo vuoto". web_user_to_shim fa
        # bool(valore) sul dato grezzo: una stringa non vuota (es. "false")
        # diventa True per come funziona bool() in Python, non per un booleano
        # vero mandato da IG. Il tipo va validato sul payload GREZZO, prima
        # della conversione dello shim, altrimenti la guardia arriva a valle
        # di un valore gia' (erroneamente) convertito.
        for campo in ("is_private", "is_verified"):
            grezzo = payload.get(campo) if isinstance(payload, dict) else None
            if not isinstance(grezzo, bool):
                continue
            nuovo = getattr(shim, campo, None)
            if isinstance(nuovo, bool) and getattr(follower, campo, None) != nuovo:
                setattr(follower, campo, nuovo)
                scritto = True

        if not scritto:
            return False
        follower.updated_at = datetime.utcnow()
        await db.commit()
        return True
    except IntegrityError as e:
        # La SELECT preventiva qui sopra esclude la collisione al momento in cui
        # guarda, ma fra quella lettura e il commit un altro worker puo' scrivere
        # la stessa targa: la finestra e' reale e resta aperta. Non la chiudiamo
        # con un savepoint perche' il rollback del savepoint scadrebbe gli
        # attributi ORM di `follower`, e rileggerli qui significherebbe un lazy
        # load dentro il gestore d'errore di una funzione che promette di non
        # sollevare mai — si comprerebbe una corsa rara al prezzo di un guasto
        # peggiore.
        #
        # La si rende invece LEGGIBILE. Senza questo ramo il messaggio sarebbe
        # "scrittura saltata", identico a quello di un DB irraggiungibile: la
        # causa vera non si distinguerebbe mai dai log. Conseguenza dell'evento:
        # la targa resta provvisoria su questa riga (l'harvest non viene
        # richiamato per lo stesso DM). Non si perde nessun contatto e non si
        # corrompe niente — dopo questo cantiere una targa provvisoria e' una
        # chiave legittima, non una riga di serie B.
        logger.error(
            f"[Harvest] @{username}: targa non ancorata, un'altra riga ha preso "
            f"lo stesso pk fra la verifica e il commit. Resta provvisoria: {e}"
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return False
    except Exception as e:
        # warning e non debug: un guasto qui gira DOPO che il DM e' partito e non
        # tocca la contabilita' dell'invio, ma se resta a debug nessuno lo vede mai
        # (stessa lezione del Task 10 sulla cattura passiva).
        logger.warning(f"[Harvest] scrittura saltata per @{username}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return False
