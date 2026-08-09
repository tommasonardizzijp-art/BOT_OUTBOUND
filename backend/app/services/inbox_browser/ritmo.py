"""Pause fra una chat e l'altra, differenziate per zona.

Il ritmo NON e' uniforme sulla lista: pause piene dove si aprono chat nuove,
ritmo rapido dove si attraversa una zona gia' lavorata. E' quello che fa una
persona: scorre in fretta cio' che ha gia' visto e si ferma su cio' che le
interessa. Il throughput dipende quasi per intero da qui — l'apertura di una chat
costa mezzo secondo (misurato), le pause costano dieci volte tanto.

La distribuzione e' lognormale TRONCATA per riestrazione, mai clampata: il clamp
accumula la coda esattamente sui bound (misurato sul motore API: il 45% dei
ritardi finiva su due valori fissi), e due picchi netti sono una firma piu'
riconoscibile di un ritardo costante.
"""
from __future__ import annotations

import math
import random

PARAMETRI: dict[str, dict] = {
    # zona piena: si aprono chat nuove, ci si ferma a leggere
    "piena": {
        "normale": (1.0, 4.0),
        "sosta": (10.0, 30.0),
        "stacco": (120.0, 300.0),
        "p_sosta": 0.10,
        "p_stacco": 0.02,
    },
    # zona rapida: si attraversa cio' che e' gia' stato raccolto
    "rapida": {
        "normale": (0.4, 1.2),
        "sosta": (10.0, 30.0),
        "stacco": (120.0, 300.0),
        "p_sosta": 0.025,
        "p_stacco": 0.02,
    },
}

SIGMA = 0.9


def _troncata(lo: float, hi: float, sigma: float = SIGMA) -> float:
    """Lognormale troncata su [lo, hi] per riestrazione.

    Mediana sulla media geometrica sqrt(lo*hi): centro naturale in scala
    logaritmica, quindi il troncamento taglia code simmetriche e accetta circa
    due volte su tre.
    """
    if hi <= lo:
        return float(lo)
    mediana = math.sqrt(lo * hi)
    for _ in range(20):
        d = random.lognormvariate(0, sigma) * mediana
        if lo <= d <= hi:
            return d
    return mediana


def campiona_pausa(zona: str) -> float:
    """Secondi di attesa prima della prossima riga. Solleva KeyError su zona ignota."""
    p = PARAMETRI[zona]
    sorte = random.random()
    if sorte < p["p_stacco"]:
        return _troncata(*p["stacco"])
    if sorte < p["p_stacco"] + p["p_sosta"]:
        return _troncata(*p["sosta"])
    return _troncata(*p["normale"])
