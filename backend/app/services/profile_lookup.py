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
