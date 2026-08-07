import pytest

from app.workers import cron_worker


@pytest.mark.asyncio
async def test_deadman_ping_disabilitato_senza_url(monkeypatch):
    """Vuoto = disabilitato (review P6, 07/08): nessun URL configurato non
    deve fallire ne' provare a chiamare nulla."""
    monkeypatch.setattr(cron_worker.settings, "wa_deadman_ping_url", "")
    esito = await cron_worker.wa_deadman_ping({})
    assert esito == {"inviato": False, "motivo": "non_configurato"}


@pytest.mark.asyncio
async def test_deadman_ping_chiama_url_configurato(monkeypatch):
    chiamate = []

    class _RespOk:
        def raise_for_status(self):
            pass

    class _ClientFinto:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            chiamate.append(url)
            return _RespOk()

    monkeypatch.setattr(cron_worker.settings, "wa_deadman_ping_url",
                        "https://hc-ping.com/token-finto")
    monkeypatch.setattr(cron_worker.httpx, "AsyncClient", lambda **kw: _ClientFinto())

    esito = await cron_worker.wa_deadman_ping({})
    assert esito == {"inviato": True}
    assert chiamate == ["https://hc-ping.com/token-finto"]


@pytest.mark.asyncio
async def test_deadman_ping_fallito_non_solleva(monkeypatch):
    """Un ping fallito non deve fermare il cron worker -- e' proprio
    l'assenza di ping, rilevata dal servizio esterno, il segnale che conta."""
    class _ClientRotto:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise ConnectionError("rete giu'")

    monkeypatch.setattr(cron_worker.settings, "wa_deadman_ping_url",
                        "https://hc-ping.com/token-finto")
    monkeypatch.setattr(cron_worker.httpx, "AsyncClient", lambda **kw: _ClientRotto())

    esito = await cron_worker.wa_deadman_ping({})
    assert esito == {"inviato": False, "motivo": "ConnectionError"}
