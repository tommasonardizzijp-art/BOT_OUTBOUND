"""Parsing del testo dell'inbox web: funzioni pure, nessun browser, nessun DB.

Qui vive il fallimento piu' insidioso del modulo. Le stringhe che leggiamo
dipendono dalla LINGUA DELL'INTERFACCIA DELL'ACCOUNT, non da una nostra
impostazione. Se il prefisso "Tu:" non viene riconosciuto perche' l'account e' in
inglese, OGNI chat risulta "ha risposto": nessun errore, solo dati falsi che poi
guidano anche i diversivi anti-ban.

Per questo `ultimo_nostro` e' un tri-stato: True / False / None. None significa
"non lo so" e non va mai confuso con False.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

LINGUE: dict[str, dict[str, str]] = {
    "it": {
        "prefisso_nostro": "Tu:",
        "segnaposto": "utente instagram",
        "campo_scrittura": "Scrivi un messaggio...",
    },
    "en": {
        "prefisso_nostro": "You:",
        "segnaposto": "instagram user",
        "campo_scrittura": "Message...",
    },
}

_SEGNAPOSTO = {v["segnaposto"] for v in LINGUE.values()}
_INVISIBILI = re.compile(r"[\u200b-\u200f\u2028-\u202f\ufeff]")
_SPAZI = re.compile(r"\s+")


def normalizza_nome(nome: str | None) -> str:
    """Forma canonica per il confronto: minuscolo, senza emoji, spazi compattati.

    Gli emoji vanno tolti perche' Instagram li lascia nei nomi profilo e la
    stessa persona puo' comparire con o senza a seconda di dove leggiamo.
    """
    if not nome:
        return ""
    testo = _INVISIBILI.sub("", str(nome))
    testo = "".join(c for c in testo if unicodedata.category(c) not in ("So", "Sk", "Cf"))
    return _SPAZI.sub(" ", testo).strip().lower()


def e_segnaposto(nome: str | None) -> bool:
    """Profilo cancellato o disattivato: si ignora senza aprire la chat."""
    return normalizza_nome(nome) in _SEGNAPOSTO


@dataclass
class RigaLista:
    nome: str | None
    anteprima: str | None
    ultimo_nostro: bool | None   # None = lingua non riconosciuta, NON "ha risposto"
    data_relativa: str | None


def analizza_riga_lista(testo_riga: str, lingua: str) -> RigaLista:
    """Scompone il testo di una riga della lista chat.

    Solleva KeyError se la lingua non e' prevista: meglio fermarsi che indovinare.
    """
    voci = LINGUE[lingua]
    righe = [r.strip() for r in (testo_riga or "").split("\n") if r.strip()]
    if not righe:
        return RigaLista(None, None, None, None)

    nome = righe[0]
    anteprima = righe[1] if len(righe) > 1 else None
    data = righe[-1] if len(righe) > 2 else None

    ultimo_nostro: bool | None = None
    if anteprima:
        if anteprima.startswith(voci["prefisso_nostro"]):
            ultimo_nostro = True
        elif any(anteprima.startswith(v["prefisso_nostro"]) for v in LINGUE.values()):
            # Il prefisso c'e' ma e' di un'ALTRA lingua: l'interfaccia non e'
            # quella che credevamo. Dichiarare False qui significherebbe marcare
            # come "ha risposto" un messaggio nostro.
            ultimo_nostro = None
        else:
            ultimo_nostro = False

    return RigaLista(nome=nome, anteprima=anteprima, ultimo_nostro=ultimo_nostro, data_relativa=data)


def estrai_username_thread(href_list: list[str], propri: set[str]) -> str | None:
    """Lo username dell'interlocutore dagli href a segmento singolo.

    Ritorna None se i candidati sono zero o PIU' DI UNO: piu' candidati significa
    thread di gruppo, menzione o post condiviso, e prendere "l'ultimo" salverebbe
    la persona sbagliata senza nessun errore.
    """
    servizio = {"reels", "explore", "direct", "stories", "p", "accounts"}
    candidati = []
    for href in href_list or []:
        parti = [p for p in (href or "").split("/") if p]
        if len(parti) != 1:
            continue
        u = parti[0].lower()
        if u in servizio or u in {p.lower().lstrip("@") for p in propri}:
            continue
        if u not in candidati:
            candidati.append(u)
    return candidati[0] if len(candidati) == 1 else None


def estrai_ultimo_messaggio(testo_pagina: str, lingua: str) -> str | None:
    """L'ultimo messaggio della conversazione: la riga prima del campo di scrittura."""
    delimitatore = LINGUE[lingua]["campo_scrittura"]
    righe = [r.strip() for r in (testo_pagina or "").split("\n") if r.strip()]
    try:
        i = len(righe) - 1 - righe[::-1].index(delimitatore)
    except ValueError:
        return None
    return righe[i - 1] if i > 0 else None


_MESI: dict[str, dict[str, int]] = {
    "it": {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
           "lug": 7, "ago": 8, "set": 9, "sett": 9, "ott": 10, "nov": 11, "dic": 12},
    "en": {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12},
}


def estrai_data_thread(testo_pagina: str, lingua: str) -> "datetime | None":
    """Data assoluta dal thread aperto, es. '9 feb 2026, 20:28' (IT).

    None se il formato non combacia: mai indovinare una data. Solleva KeyError
    su lingua non prevista, coerente con le altre funzioni di questo modulo.
    """
    mesi = _MESI[lingua]
    m = re.search(
        r"(\d{1,2})\s+([A-Za-zÀ-ù]{3,4})\.?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})",
        testo_pagina or "",
    )
    if not m:
        return None
    giorno, mese_str, anno, ora, minuto = m.groups()
    mese = mesi.get(mese_str.lower())
    if mese is None:
        return None
    try:
        return datetime(int(anno), mese, int(giorno), int(ora), int(minuto))
    except ValueError:
        return None
