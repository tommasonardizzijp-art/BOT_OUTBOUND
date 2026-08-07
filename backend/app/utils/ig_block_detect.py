# backend/app/utils/ig_block_detect.py
"""Riconosce dall'URL se il browser e' finito su una pagina di blocco Instagram.

Perche' esiste: il 07/08/2026 e' comparso `instagram.com/accounts/scraping_warning/`
— l'anti-scraping di Meta. Il bot non aveva modo di accorgersene durante il lavoro
(il controllo su challenge/checkpoint esisteva SOLO nel login) e avrebbe continuato
a chiedere pagine da dietro l'avviso, marcando ogni profilo come 'non trovato'.
Continuare a lavorare da dietro un avviso e' anche la prova piu' netta che si possa
dare a Meta che dall'altra parte non c'e' una persona: un umano si ferma.

Il confronto e' sui SEGMENTI del path, non sull'URL come stringa:
  - `@challenge_accepted` ha path `/challenge_accepted/` e NON e' un blocco;
  - l'URL del warning porta `challenge_context=...` nella query, quindi cercare
    'challenge' nell'URL intero darebbe il nome del blocco sbagliato.
"""
from urllib.parse import urlparse

# Segmenti di path che identificano una pagina di blocco. Il valore ritornato e'
# il segmento stesso, cosi' i log dicono QUALE blocco e' scattato.
_BLOCK_SEGMENTS = frozenset({
    "scraping_warning",  # /accounts/scraping_warning/ — anti-scraping (07/08/2026)
    "challenge",         # /challenge/... — verifica identita'
    "checkpoint",        # /checkpoint/... — checkpoint classico
    "suspended",         # /accounts/suspended/ — account sospeso
})


def detect_block_interstitial(url: str | None) -> str | None:
    """Nome del blocco se `url` e' una pagina di blocco IG, altrimenti None.
    Non solleva mai: un URL malformato non e' un blocco."""
    if not url:
        return None
    try:
        path = urlparse(url).path
    except ValueError:
        return None
    for segment in path.split("/"):
        if segment in _BLOCK_SEGMENTS:
            return segment
    return None
