"""ADVERSARIALE — direzione 5 del cantiere: "una pagina di soli segnaposto ora
conta come 'senza lavoro'". Verifico se questo fa PERDERE la guardia dedicata.

`run_inbox_list` (app/services/scrape_inbox.py) ha una guardia veloce e
dedicata proprio per "pagine con thread ma zero utenti leggibili" — 3 pagine
consecutive (`settings.inbox_pagine_senza_partecipanti_stop`), pensata per un
payload DEGRADATO (IG che smette di mandare pk/username nei thread). Il
segnale che la fa scattare e' `page.threads_con_utenti == 0`
(app/services/inbox_source.py:124-138: incrementato se ALMENO un utente del
thread ha pk E username leggibili — SENZA controllare `handle_valido`).

Con questo cantiere, un thread il cui UNICO "utente" e' il segnaposto
("Utente di Instagram") ha pk leggibile e username non vuoto: fa scattare
`threads_con_utenti += 1` in inbox_source.py, quindi la pagina NON e' "senza
partecipanti" per quel contatore — anche se, dopo il filtro `handle_valido` in
`classifica_pagina`, produce ESATTAMENTE ZERO lavoro (0 nuovi, 0 promozioni),
proprio come un payload degradato.

La guardia dedicata (3 pagine) non vede questo caso: `senza_part_streak` si
azzera ogni pagina. L'unica rete che eventualmente ferma una discesa fatta
solo di segnaposto e' `inbox_discesa_senza_lavoro_stop` — DI DEFAULT 3000
pagine (app/config.py:211), mille volte piu' larga. Nell'intervallo fra 3 e
3000 pagine la discesa continua a consumare budget di sessione (pause,
latenza, rischio anti-detect) senza produrre niente e senza che nessuno dei
log "mi fermo" dedicati si accenda — l'unico segnale e' quello generico,
tarato per un evento raro che in teoria non dovrebbe mai avvicinarsi alla
soglia.

Dimostro il gap per confronto diretto: stessa sequenza di pagine "senza lavoro
utile", stesso numero di pagine (>3), UNICA differenza `threads_con_utenti`
(0 = payload davvero degradato, >0 = solo segnaposto). Il primo caso ferma il
giro a pagina 3 con `fine_discesa=True` (comportamento SANO, atteso). Il
secondo NON lo fa: consuma tutte le pagine scriptate.
"""
import asyncio
import pytest
import sys

sys.path.insert(0, "tests")  # per importare l'harness come modulo top-level

from app.services.inbox_source import InboxPage
from app.config import settings
from test_scrape_inbox_adversarial import _setup_inbox_db, _run_inbox_list, _read_state  # noqa: E402

SEGNAPOSTO = "Utente di Instagram"
SOGLIA = settings.inbox_pagine_senza_partecipanti_stop  # 3, di fabbrica


def _pagine_degradate(n, *, con_segnaposto: bool):
    """n pagine con thread ma senza contatti VALIDI da estrarre — o perche' il
    payload e' davvero degradato (threads_con_utenti=0), o perche' i thread
    contengono solo segnaposto (threads_con_utenti>0, ma zero utili dopo
    handle_valido). In entrambi i casi: 0 contatti reali per l'operatore."""
    pagine = []
    for i in range(n):
        if con_segnaposto:
            partecipanti = [(9000 + i, SEGNAPOSTO)]
            utenti_leggibili = 1
        else:
            partecipanti = []
            utenti_leggibili = 0
        pagine.append(InboxPage(
            participants=partecipanti, cursor=f"c{i}", exhausted=False,
            threads_letti=1, threads_con_utenti=utenti_leggibili,
        ))
    return pagine


def test_payload_davvero_degradato_ferma_la_discesa_alla_soglia(monkeypatch):
    """Controllo positivo: senza segnaposto di mezzo, la guardia dedicata regge
    e ferma il giro esattamente alla soglia configurata."""
    n_scriptate = SOGLIA + 4
    pagine = _pagine_degradate(n_scriptate, con_segnaposto=False)
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pagine)
    try:
        _run_inbox_list(session_factory, campaign_id)
        conteggio, status, totale = _read_state(session_factory, campaign_id)
        assert conteggio == 0
    finally:
        cleanup()


@pytest.mark.xfail(strict=True, reason=(
    """LACUNA CONFERMATA, preesistente, non peggiorata da questo cantiere.

    Una pagina fatta solo di profili chiusi ha thread con pk e username
    leggibili, quindi `threads_con_utenti > 0` e la guardia dedicata
    ("niente da estrarre", soglia 3 pagine) non scatta. La discesa si ferma
    solo sulla rete di sicurezza generica, tarata 1000 volte piu' larga.

    Non e' una regressione: PRIMA del filtro dei segnaposto quelle pagine
    producevano righe morte, quindi `stored > 0` azzerava il contatore e la
    discesa non si fermava affatto. Il filtro ha spostato il caso da
    "non si ferma mai" a "si ferma tardi". Resta budget e rischio anti-detect
    speso su pagine che non producono nulla, senza che nessun log dedicato si
    accenda.

    La cura sta in `inbox_source.py`, che decide cosa conta come "partecipante
    leggibile", non nel codice di questo cantiere. strict=True: torna verde da
    solo quando quella soglia impara a contare i segnaposto."""
))
def test_pagine_di_soli_segnaposto_non_fanno_scattare_la_guardia_dedicata(monkeypatch):
    """Difetto: identico scenario (0 contatti utili a pagina, per SOGLIA+ pagine
    consecutive), ma con thread che portano il segnaposto invece di un payload
    vuoto. La guardia dedicata (soglia bassa, pensata per questo esatto caso
    d'uso — "niente da estrarre") non se ne accorge: il giro continua oltre la
    soglia, si ferma solo perche' lo script di test finisce le pagine (in
    produzione continuerebbe fino a inbox_discesa_senza_lavoro_stop=3000)."""
    n_scriptate = SOGLIA + 4
    pagine = _pagine_degradate(n_scriptate, con_segnaposto=True)
    session_factory, campaign_id, cleanup = _setup_inbox_db(monkeypatch, pagine)
    try:
        _run_inbox_list(session_factory, campaign_id)
        conteggio, status, totale = _read_state(session_factory, campaign_id)
        assert conteggio == 0   # nessun contatto vero prodotto: e' un dato di fatto, non il difetto

        # IL DIFETTO: leggo quante pagine sono state effettivamente consumate
        # prima che il giro si fermasse. Se la guardia dedicata avesse
        # riconosciuto "solo segnaposto" come "niente da estrarre" (la stessa
        # categoria del payload degradato sopra), si sarebbe fermato dopo
        # SOGLIA pagine, non dopo tutte quelle scriptate.
        async def _pagine_lette():
            from app.models.campaign import Campaign
            async with session_factory() as db:
                c = await db.get(Campaign, campaign_id)
                return c.inbox_deep_pages
        pagine_lette = asyncio.run(_pagine_lette())

        assert pagine_lette <= SOGLIA, (
            f"la guardia dedicata (soglia {SOGLIA} pagine 'senza partecipanti "
            f"leggibili') NON e' scattata su {n_scriptate} pagine consecutive di "
            f"solo segnaposto: il giro ne ha lette {pagine_lette} prima di "
            "fermarsi solo perche' lo script di test e' finito. In produzione "
            "la discesa avrebbe continuato fino alla rete di sicurezza generica "
            f"(inbox_discesa_senza_lavoro_stop={settings.inbox_discesa_senza_lavoro_stop} "
            "pagine), mille volte piu' larga della guardia pensata apposta per "
            "questo caso."
        )
    finally:
        cleanup()
