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
        scritto = False
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
