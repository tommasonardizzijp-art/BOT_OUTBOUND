"""Orologio virtuale per i test del canale WhatsApp.

Modulo normale e non un conftest, come factories_wa.py: serve a piu' file di
test (worker, adversarial M4, blocker M5.1) e nessuno di loro ha motivo di
toccare tests/conftest.py, congelato da PR-0.

**Perche' esiste.** Da M5.1 la mini-sessione ASPETTA la quarantena di
risincronizzazione prima del primo invio: `WA_RESYNC_QUARANTINE_MIN` minuti,
15 di default. Senza un orologio finto ogni test che apre il browser dormirebbe
davvero un quarto d'ora.

**Nota di metodo, che vale piu' del codice.** La scorciatoia ovvia sarebbe
azzerare `WA_RESYNC_QUARANTINE_MIN` nei test. E' esattamente cio' che ha
tenuto nascosto per un mese il difetto che impediva ogni invio: i test del
sender passavano `browser_avviato_da_s=9999` o azzeravano la quarantena in 27
casi su 27, quindi nessuno ha mai visto che nella realta' non scadeva mai.
La quarantena dev'essere ATTRAVERSATA dai test con i suoi valori veri --
soltanto in fretta. Da qui l'orologio virtuale invece della config azzerata.
"""
import math


def orologio_virtuale(modulo, monkeypatch, orologio: dict | None = None) -> dict:
    """Tempo virtuale condiviso da `time.perf_counter` e `asyncio.sleep` dentro
    `modulo`: `sleep` non dorme, fa avanzare l'orologio.

    `modulo` e' il modulo sotto test (di norma `app.workers.wa_worker`): si
    sostituiscono i suoi riferimenti, non quelli globali, cosi' il resto della
    suite continua a vedere il tempo vero.

    Ritorna il dizionario dell'orologio (`{"t": secondi}`), che il chiamante
    puo' leggere per verificare quanto tempo virtuale e' passato, o passare
    gia' pronto quando vuole farlo avanzare anche da parte sua.
    """
    orologio = orologio if orologio is not None else {"t": 0.0}

    async def _sleep(secondi):
        orologio["t"] += secondi

    monkeypatch.setattr(modulo.time, "perf_counter", lambda: orologio["t"])
    monkeypatch.setattr(modulo.asyncio, "sleep", _sleep)
    return orologio


def fette_di_quarantena() -> int:
    """Quante volte l'attesa della quarantena rinnova il lucchetto profilo.
    Calcolata dalla config e non scritta a mano: cambiare il default non deve
    rompere i test che contano i rinnovi."""
    from app.config import settings
    from app.workers import wa_worker

    return math.ceil(settings.wa_resync_quarantine_min * 60
                     / wa_worker.FETTA_ATTESA_QUARANTENA_S)
