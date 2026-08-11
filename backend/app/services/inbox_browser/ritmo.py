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
    #
    # Lo stacco resta — una sessione senza nessuna pausa lunga e' una firma —
    # ma scende da (120-300s, 2%) a (90-180s, 1%) con il via libera esplicito
    # di Tommaso l'11/08 (Task 11): il costo atteso per apertura passa da 4.2s
    # a 1.35s. Misurato l'11/08: due stacchi si mangiavano il 24% di una
    # sessione da 30 minuti. Il session-break da 30-55 minuti, che e' la
    # pausa lunga vera, non e' toccato.
    "piena": {
        "normale": (1.0, 4.0),
        "sosta": (10.0, 30.0),
        "stacco": (90.0, 180.0),
        "p_sosta": 0.10,
        "p_stacco": 0.01,
    },
    # zona rapida: si attraversa cio' che e' gia' stato raccolto
    "rapida": {
        "normale": (0.4, 1.2),
        "sosta": (10.0, 30.0),
        "stacco": (90.0, 180.0),
        "p_sosta": 0.025,
        "p_stacco": 0.01,
    },
    # solo scorrimento: la riga e' gia' nota e NON viene aperta.
    # Qui non parte nessuna richiesta verso Instagram — nessun click, nessun
    # thread caricato: l'unica cosa che l'altra parte vede e' lo scroll, che ha
    # gia' il suo ritmo umano (pagina.piano_scroll). Prendersi qui una pausa da
    # cinque minuti non abbassa di un byte il footprint, abbassa solo il
    # throughput, e per giunta e' meno umano del contrario: nessuno scorre cento
    # nomi e poi si blocca cinque minuti a meta' lista.
    # Misurato l'11/08: 144 righe su 170 finivano qui, e si portavano via la
    # maggior parte dei 972s di sleep di una sessione da 18 minuti.
    "scorrimento": {
        "normale": (0.25, 0.9),
        "sosta": (6.0, 18.0),
        "stacco": (6.0, 18.0),   # mai estratto (p_stacco=0): la sosta e' il tetto
        "p_sosta": 0.025,
        "p_stacco": 0.0,
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


def zona_pausa(zona: str, ha_aperto: bool) -> str:
    """Con che ritmo aspettare dopo questa riga.

    La distinzione che conta non e' fra zona piena e zona rapida: e' fra
    l'aver COMPIUTO un'azione (aprire una chat, che Instagram vede) e l'aver
    solo scorso una riga gia' nota (che Instagram non vede affatto). Le pause
    lunghe appartengono alle azioni; applicarle anche allo scorrimento vuol
    dire pagarne il prezzo senza comprarne la protezione.
    """
    return zona if ha_aperto else "scorrimento"


def campiona_pausa(zona: str) -> float:
    """Secondi di attesa prima della prossima riga. Solleva KeyError su zona ignota."""
    p = PARAMETRI[zona]
    sorte = random.random()
    if sorte < p["p_stacco"]:
        return _troncata(*p["stacco"])
    if sorte < p["p_stacco"] + p["p_sosta"]:
        return _troncata(*p["sosta"])
    return _troncata(*p["normale"])
