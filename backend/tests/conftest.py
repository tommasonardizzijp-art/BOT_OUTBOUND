"""Guardia globale: i test non devono MAI mandare messaggi Telegram reali.

Il .env di sviluppo contiene TELEGRAM_BOT_TOKEN/CHAT_ID veri: un test che
esercita un path del notifier senza mock spamma l'operatore su Telegram
(successo il 06/07/2026 — test_bio_error_no_infinite_loop non mockava
send_scrape_warning_alert e ogni run pytest inviava alert "proxy instabile"
con campagne finte). Qui _telegram_enabled viene spento per ogni test.

I test che verificano l'invio (es. test_notifier.py) continuano a funzionare:
il loro monkeypatch.setattr nel corpo del test sovrascrive questo fixture.
"""
import pytest

from app.services import notifier


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    monkeypatch.setattr(notifier, "_telegram_enabled", lambda: False)
