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
        # e' esplicito, e non contano come "campo vuoto".
        for campo in ("is_private", "is_verified"):
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
        logger.warning(f"[Harvest] scrittura saltata per @{getattr(follower, 'username', '?')}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return False
