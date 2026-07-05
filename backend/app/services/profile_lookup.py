"""Come chiamare il profilo in modo app-like. Puro/testabile, niente IO qui in cima.

Il bare `user_info_v1(pk)` usa `from_module="self_profile"`: ogni lookup su profili
ALTRUI dichiara "sto guardando il MIO profilo", migliaia di volte = firma da bot. Un
apertura profilo reale (da feed/reel) usa un modulo diverso, che instagrapi traduce in
`entry_point="profile"`. Valori validi (assert instagrapi INFO_FROM_MODULES): self_profile
| feed_timeline | reel_feed_timeline. Qui scegliamo un modulo realistico non-self.
"""
import random

# Moduli realistici per l'apertura di un profilo ALTRUI (non self_profile).
_REALISTIC_MODULES = ("feed_timeline", "reel_feed_timeline")
# Pesato: la gran parte delle aperture profilo arriva dal feed principale.
_WEIGHTS = (0.85, 0.15)


def pick_from_module() -> str:
    """Ritorna un from_module realistico per l'apertura di un profilo altrui."""
    return random.choices(_REALISTIC_MODULES, weights=_WEIGHTS, k=1)[0]


def fetch_profile_app_like(client, pk: str):
    """Recupera il profilo come farebbe l'app aprendo un contatto.

    RAMO B (default, tutto instagrapi-supported): user_info_v1 con from_module realistico
    (non self_profile) + fetch di pochi post del profilo (feed/user), che l'app carica
    sempre all'apertura di un profilo. I post NON vengono salvati: servono solo a far
    sembrare la chiamata un'apertura profilo vera, non un bare user_info.

    Ritorna l'oggetto User (stesso tipo di user_info_v1) -> storage/extract_contacts invariati.

    RAMO A (solo se lo spike conferma full_detail_info): sostituire il corpo con
    client.private_request(f"users/{pk}/full_detail_info/") + extract_user_v1(res["user_detail"]["user"]),
    mantenendo il fetch post e il fallback qui sotto.
    """
    from loguru import logger

    user = client.user_info_v1(pk, from_module=pick_from_module())
    # Post-grid come l'app (best-effort, scartati): non deve MAI rompere la bio.
    try:
        client.user_medias_v1(pk, amount=12)  # prima pagina della griglia post
    except Exception as e:
        logger.debug(f"[ProfileLookup] fetch post best-effort fallito per {pk}: {e}")
    return user
