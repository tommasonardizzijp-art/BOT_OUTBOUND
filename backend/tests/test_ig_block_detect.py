# backend/tests/test_ig_block_detect.py
"""Riconoscimento delle pagine di blocco IG dall'URL.

Il caso che conta davvero e' l'ultimo gruppo: un profilo il cui USERNAME
contiene 'challenge'/'checkpoint' non deve fermare il bot. Un falso positivo
qui costa quanto un falso negativo: isola un account sano e mette in pausa
una campagna che stava lavorando.
"""
import pytest

from app.utils.ig_block_detect import detect_block_interstitial

# URL reale ricevuto il 07/08/2026 (challenge_context troncato).
WARNING_URL = (
    "https://www.instagram.com/accounts/scraping_warning/"
    "?challenge_context=Q9y1BQI2WZ7jFctCbIdo7f1QtsX544BXnz&next=https%3A%2F%2Fwww.instagram.com%2F"
)


@pytest.mark.parametrize("url,atteso", [
    (WARNING_URL, "scraping_warning"),
    ("https://www.instagram.com/accounts/scraping_warning/", "scraping_warning"),
    ("https://www.instagram.com/challenge/?next=/", "challenge"),
    ("https://www.instagram.com/challenge/action/12345/", "challenge"),
    ("https://www.instagram.com/checkpoint/", "checkpoint"),
    ("https://www.instagram.com/accounts/suspended/", "suspended"),
])
def test_riconosce_le_pagine_di_blocco(url, atteso):
    assert detect_block_interstitial(url) == atteso


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/mario_rossi/",
    "https://www.instagram.com/direct/t/1234567890/",
    "https://www.instagram.com/",
    "https://www.instagram.com/p/ABC123/",
])
def test_le_pagine_normali_non_sono_blocchi(url):
    assert detect_block_interstitial(url) is None


@pytest.mark.parametrize("url", [
    # Username che CONTENGONO la parola chiave: non sono blocchi.
    "https://www.instagram.com/challenge_accepted/",
    "https://www.instagram.com/checkpoint_charlie/",
    "https://www.instagram.com/suspended_animation/",
    "https://www.instagram.com/thechallenge/",
    # Parola chiave solo nella query string di una pagina normale.
    "https://www.instagram.com/mario_rossi/?challenge_context=abc",
])
def test_nessun_falso_positivo_su_username_o_query(url):
    assert detect_block_interstitial(url) is None


@pytest.mark.parametrize("url", [None, "", "non-un-url", "://rotto"])
def test_input_degeneri_non_sollevano(url):
    assert detect_block_interstitial(url) is None
