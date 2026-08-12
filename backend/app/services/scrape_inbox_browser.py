# backend/app/services/scrape_inbox_browser.py
"""Fase Lista via browser per scrape_mode=dm_threads, inbox_engine=browser.

Motore SEPARATO da scrape_inbox.py (API), che non viene toccato. Condivide solo
il governo a monte (list_followers: stato campagna, kill-switch, resume) e il
salvataggio a valle.

REGOLA FONDANTE: una riga non riconosciuta si apre SEMPRE. La zona governa solo
il ritmo. Vedi decide_se_aprire.

Lingua dell'interfaccia: hardcoded 'it' per ora. Nessun account ha un campo di
lingua configurabile (verificato: app/models/account.py non ne ha uno) e nessun
task del piano copre l'auto-rilevamento. L'unico account misurato (Task 0) è in
italiano. Su un account con interfaccia inglese il motore classificherebbe male
(vedi test_LINGUA_SBAGLIATA_non_deve_mentire in testo.py: il tri-stato protegge
da 'ha risposto' falso, ma non da un mancato riconoscimento dei segnaposto/date).
Rendere configurabile è lavoro futuro, fuori scope di questo piano.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.models.campaign import CampaignStatus
from app.models.follower import Follower
from app.services.bot_state_service import is_halted
from app.services.inbox_browser.pagina import (
    RigaVisibile, apri_riga, decidi_fine_lista, lancia, leggi_righe_visibili,
    lista_ripartita_da_capo, stato_lista,
)
from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona
from app.services.inbox_browser.ritmo import campiona_pausa, zona_pausa
from app.services.inbox_browser.salvataggio import DatiContatto, salva_contatto
from app.services.inbox_browser.segnalibro import (
    nuovo_cursore, nuovo_cursore_da_data, riga_da_saltare, soglia_in_ore,
)
from app.services.inbox_browser.testo import (
    analizza_riga_lista, e_segnaposto, estrai_data_thread, estrai_ultimo_messaggio,
    eta_riga_in_ore, normalizza_nome,
)
from app.services.scrape_inbox import _single_inbox_account   # sola lettura DB
from app.services.scraper import is_challenge_exception, isolate_challenged_account
from app.utils.exceptions import BotHaltedError, ScrapeBudgetError, ScraperError

DURATA_SESSIONE_MIN = 30 * 60
DURATA_SESSIONE_MAX = 55 * 60
LINGUA = "it"
_INBOX_ENDPOINT = re.compile(r"(direct_v2|graphql)", re.I)

# Quante volte si ritenta una riga che non si e' riusciti ad aprire, dentro la
# stessa sessione. Un fallimento di apertura NON e' un giudizio sulla riga: e'
# quasi sempre la riga uscita dal DOM mentre si scendeva. Marcarla come "gia'
# vista" (cosa che il motore faceva prima del click) la cancellava per tutta la
# sessione: 84 righe su 143 nella baseline dell'11/08 — contatti persi in
# silenzio, esattamente cio' da cui la memoria di sessione doveva proteggere.
# Il tetto esiste per non girare in tondo su una riga che non si risolve mai.
MAX_TENTATIVI_RIGA = 3

# Quanti lanci si concedono per tornare dove si era arrivati dopo che la lista
# e' ripartita da capo. A 5-9 schermate per lancio coprono decine di migliaia di
# pixel; oltre, e' piu' onesto rinunciare e lasciare che il giro normale
# riattraversi la zona (le righe gia' viste non si ripagano, vedi gia_esaminata).
MAX_LANCI_RECUPERO = 40

# Ogni quanto si interroga davvero il DB per sapere se bisogna fermarsi.
# `_deve_fermarsi` costa DUE viaggi verso il Postgres condiviso (kill-switch +
# rilettura campagna): misurati **349 ms per riga** sul pooler Supabase il
# 12/08. Chiamandolo a ogni riga si mangiava il 52% di una sessione (342s su
# 663s misurati) — piu' del doppio di tutte le pause anti-ban messe insieme.
# Il controllo a ogni riga non comprava reattivita': una riga in "piena" dorme
# comunque da 1 a 90 secondi, e quel sonno non e' interrompibile. Con la
# soglia a tempo il ritardo dello stop resta dominato dal sonno in corso,
# esattamente come prima, e le righe attraversate in fretta (zona gia'
# lavorata, 0.15-0.5s l'una) smettono di pagare un viaggio di rete a testa.
INTERVALLO_CONTROLLO_STOP_S = 2.0


def gia_esaminata(chiave: str | None, viste: set[str]) -> bool:
    """True se questa riga e' gia' passata sotto gli occhi in QUESTA sessione.

    Serve a non pagarne la pausa una seconda volta: il ciclo rilegge le righe
    visibili a ogni giro e `human_click` sposta la lista a ogni apertura, quindi
    il lotto successivo ricomincia in mezzo al precedente. Una riga senza chiave
    non e' memorizzabile e viene trattata come nuova: dire il contrario
    zittirebbe tutte le righe anonime dopo la prima.

    A differenza di `ArchivioNomi` (che tratta i nomi duplicati come mai
    riconosciuti, perche' i nomi visualizzati di Instagram non sono univoci),
    questa memoria non gestisce le collisioni: due chat diverse con lo stesso
    nome, nello stesso giro, fanno saltare la seconda. Rischio accettato e
    limitato alla sessione corrente (30-55 min): al giro successivo la memoria
    e' vuota e la riga torna esaminabile.
    """
    if not chiave:
        return False
    return chiave in viste


def decide_se_aprire(nome: str | None, archivio: ArchivioNomi, zona: str) -> bool:
    """REGOLA FONDANTE del modulo.

    Una riga si apre SEMPRE, tranne in due casi: e' gia' riconosciuta, oppure e'
    un segnaposto (profilo cancellato). La ZONA non compare in questa decisione,
    ed e' voluto: nel disegno precedente la zona 'rapida' non apriva niente, e
    questo faceva raccogliere ZERO contatti a regime, perche' in cima alla lista
    ci sono sempre i DM appena inviati (tutti noti).

    Il parametro `zona` resta nella firma solo per rendere esplicito che non
    influenza il risultato: non rimuoverlo pensando sia inutile, e non usarlo.
    """
    if e_segnaposto(nome):
        return False
    return not archivio.e_riconosciuto(nome)


def richiede_apertura(riga, archivio: ArchivioNomi, soglia: float | None,
                      salta_lavorate: bool, zona: str) -> bool:
    """Questa riga va aperta? UNA sola risposta, usata in due punti.

    La usa il ciclo per decidere cosa fare della riga, e la usa `raccogli`
    DURANTE il gesto di scorrimento per sapere se fermarlo (vedi
    pagina.scorri_leggendo: una riga si puo' aprire solo mentre e' a schermo).
    Se le due risposte divergessero, il gesto si fermerebbe dove non serve o —
    molto peggio — tirerebbe dritto proprio dove serviva, e la riga sarebbe
    persa. Percio' stanno qui, non duplicate.
    """
    if riga.non_letta:
        return False
    analizzata = analizza_riga_lista(riga.testo_grezzo, LINGUA)
    if riga_da_saltare(analizzata.data_relativa, soglia, salta_lavorate):
        return False
    return decide_se_aprire(riga.nome, archivio, zona)


def motore_ancora_nostro(campaign) -> bool:
    """True se la campagna e' ancora impostata sul motore browser.

    NOTA per l'implementer: questa funzione appartiene davvero al Task 14
    (auto-defer su cambio motore a caldo), non a questo task. E' definita qui,
    minimale, perche' il ciclo di QUESTO task la deve gia' chiamare (vedi sotto)
    per non lasciare una sessione da 30-55 minuti a girare a vuoto su un motore
    che l'utente ha spento nel frattempo. Il Task 14 la testera' a fondo e potra'
    estenderla; non rimuoverla né duplicarla quando arriverai a quel task.
    """
    return getattr(campaign, "inbox_engine", "api") == "browser"


async def run_inbox_browser_list(campaign_id: str, db, campaign) -> int | None:
    """Loop Fase Lista inbox via browser. Stesso contratto di run_inbox_list:
    secondi di defer al session-break (il worker solleva Retry(defer=...)),
    None se completata/interrotta."""
    from app.browser.context_manager import BrowserSession
    from app.utils.events import emit as emit_event

    account = None
    session = None
    falliti_inbox: list[str] = []
    already = await db.scalar(
        select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
    ) or 0
    since_break = 0
    nuovi_in_sessione = 0
    viste_in_sessione: set[str] = set()
    tentativi: dict[str, int] = {}
    session_started = datetime.utcnow()
    session_budget_s = random.uniform(DURATA_SESSIONE_MIN, DURATA_SESSIONE_MAX)

    ultimo_controllo_stop = float("-inf")

    async def _deve_fermarsi(forza: bool = False) -> str | None:
        """Kill-switch, stato campagna, motore ancora nostro: 'halted' | 'status'
        | 'motore' | None. Va richiamato SIA in cima al lotto SIA dentro il for
        di ogni riga (I2): con `campiona_pausa` che arriva fino a 2-5 minuti per
        riga e un lotto di 30 righe, controllare solo in cima al while farebbe
        impiegare diversi minuti al kill-switch prima di fermare davvero il
        browser — la spec chiede stop immediato.

        Il DB pero' si interroga al massimo ogni INTERVALLO_CONTROLLO_STOP_S:
        la soglia e' sul tempo TRASCORSO, quindi dopo una pausa lunga il
        controllo avviene comunque, e a saltare sono solo le chiamate in rapida
        successione (righe attraversate senza aprire). Vedi la costante per i
        numeri misurati."""
        nonlocal ultimo_controllo_stop
        adesso = time.monotonic()
        if not forza and adesso - ultimo_controllo_stop < INTERVALLO_CONTROLLO_STOP_S:
            return None
        ultimo_controllo_stop = adesso
        if await is_halted(db):
            return "halted"
        await db.refresh(campaign)
        if campaign.status not in (CampaignStatus.listing, CampaignStatus.listing_break):
            logger.info(f"[InboxBrowser] Stato '{campaign.status.value}' — interrotto a {already}")
            return "status"
        if not motore_ancora_nostro(campaign):
            logger.info("[InboxBrowser] motore cambiato durante la sessione — esco pulito")
            emit_event(campaign_id, "scrape_stopped",
                       "Motore inbox cambiato durante la raccolta: sessione interrotta", level="warn")
            return "motore"
        return None

    try:
        # _single_inbox_account puo' sollevare ScrapeBudgetError (0 o 2+ account
        # inbox attivi): DEVE stare dentro il try, altrimenti quell'eccezione
        # sfugge a tutti gli except sotto e la campagna resta in uno stato
        # inconsistente senza nessun evento emesso (visto in run_inbox_list,
        # che per lo stesso motivo include build_inbox_source nel try).
        account = await _single_inbox_account(db, campaign.id)
        session = BrowserSession(account.id)
        await session.open()
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def _on_requestfailed(req):
            try:
                if _INBOX_ENDPOINT.search(req.url):
                    falliti_inbox.append(req.url)
            except Exception:
                pass

        page.on("requestfailed", _on_requestfailed)

        # Spia a costo zero (nessuna richiesta in piu', solo un evento che
        # Playwright emette gia'): se la pagina si ricarica o naviga da sola,
        # la lista riparte dalla cima e tutto il lavoro di posizione e' perso.
        # Tommaso lo riporta come "si ricarica e si ricomincia da capo"; la
        # sonda dell'11/08 aveva catturato un `pagehide` senza poterne
        # attribuire la causa. Qui la distinzione fra "pagina ricaricata" e
        # "lista ricostruita a pagina ferma" si legge dal log, senza aggiungere
        # un solo byte di footprint.
        navigazioni: list[str] = []

        def _on_framenavigated(frame):
            try:
                if frame.parent_frame is not None:
                    return
                url = frame.url or ""
                # L'apertura di una chat porta a /direct/t/<id>/ ed e' roba
                # nostra: quella non e' una navigazione spontanea. Tutto il
                # resto — a partire da un ritorno su /direct/inbox/, che e'
                # un reload — azzera la posizione della lista.
                if "/direct/t/" in url:
                    return
                navigazioni.append(url)
                logger.warning(
                    f"[InboxBrowser] la pagina ha navigato da sola verso {url} "
                    f"(evento n.{len(navigazioni)}) — la lista riparte dalla cima"
                )
            except Exception:
                pass

        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="commit", timeout=60000)
        await page.wait_for_timeout(3000)
        if "/accounts/login" in page.url:
            raise ScraperError("Sessione web non loggata su Instagram — nessun tentativo di login automatico")

        # Registrata DOPO il goto: la navigazione che apre l'inbox e' nostra e
        # non va contata fra quelle spontanee.
        try:
            page.on("framenavigated", _on_framenavigated)
        except Exception:   # pagina finta nei test, o versione senza l'evento
            pass

        emit_event(campaign_id, "scrape_start", "Fase Lista inbox avviata (browser)")

        nomi_esistenti = (await db.execute(
            select(Follower.full_name).where(Follower.campaign_id == campaign_id)
        )).scalars().all()
        archivio = ArchivioNomi(list(nomi_esistenti))
        contatore = ContatoreZona()

        drained = False
        segnalibro_esaurito = False

        # Modalita' segnalibro (Task 7-8): attributo RUNTIME impostato dal
        # chiamante (Task 9), mai persistito — vale per una sessione sola.
        # `soglia` e' calcolata una volta sola all'ingresso: il cursore non
        # cambia durante la sessione corrente (lo aggiorna solo questa
        # sessione, a fine giro, per la PROSSIMA), quindi ricalcolarla ogni
        # giro non aggiungerebbe nulla.
        salta_lavorate = bool(getattr(campaign, "inbox_salta_lavorate", False))
        soglia = soglia_in_ore(getattr(campaign, "inbox_cursor_at", None), datetime.utcnow())
        if salta_lavorate and soglia is None:
            logger.info("[InboxBrowser] segnalibro chiesto ma nessun cursore: si legge tutto")
        if salta_lavorate and soglia is not None:
            emit_event(campaign_id, "scrape_start",
                       f"Modalita' segnalibro: si attraversa senza aprire tutto cio' che e' "
                       f"piu' recente di {soglia / 24:.1f} giorni fa")
        lanci_a_vuoto = 0

        # Righe accumulate dal campionamento DURANTE il gesto di scorrimento
        # (Task 4a): `decidi_fine_lista` ora legge la lista mentre scorre,
        # invece di scorrere alla cieca e lasciare che il giro successivo
        # rilegga da capo. Sul primo giro e' vuoto: nessuno scorrimento e'
        # ancora avvenuto, quindi si legge la posizione di partenza a parte.
        #
        # Le righe campionate nel PRIMO scorrimento di `decidi_fine_lista`
        # possono restare in coda fino a ~31s prima di essere processate, se
        # servono tutti i retry di ATTESE_S per dichiarare "continua". Prima
        # di Task 4a la finestra fra lettura e click era di un giro solo. Il
        # rischio esiste gia' ed e' gestito: `apri_riga` ri-risolve la riga
        # per NOME al momento del click, non per indice DOM (vedi la sua
        # docstring) — una riga stantia diventa "non trovata, riprovo al
        # giro dopo", mai un click sulla persona sbagliata.
        righe_del_giro: list[RigaVisibile] = []

        async def raccogli(righe_viste: list[RigaVisibile]) -> tuple[int, int]:
            """Accoda le righe mai viste. Ritorna (quante nuove, quante da aprire).

            Nessuno dei due conteggi e' decorativo. Le NUOVE dicono a
            `decidi_fine_lista` che c'e' ancora roba da lavorare, e gli
            risparmiano la scala di attese. Quelle DA APRIRE fermano il gesto
            di scorrimento sul posto (pagina.scorri_leggendo): Instagram tiene
            nel DOM solo la fascia visibile — misurato 9-10 righe il 12/08 —
            quindi una riga si puo' aprire solo mentre e' ancora sotto gli
            occhi. Tirare dritto e aprirla dopo non e' piu' lento: e'
            impossibile, e prima produceva blocchi di 20-45 fallimenti di fila.
            """
            accettate = 0
            da_aprire = 0
            for r in righe_viste:
                chiave = normalizza_nome(r.nome)
                # Senza chiave (es. nome fatto solo di emoji, che
                # normalizza_nome scarta) non si puo' sapere se e' gia' stata
                # vista: va trattata come nuova SEMPRE, stesso contratto di
                # `gia_esaminata` (Task 2) — il contrario la zittirebbe per
                # tutta la sessione, bug trovato dal QA su un nome-solo-emoji.
                if not chiave or chiave not in viste_in_sessione:
                    righe_del_giro.append(r)
                    accettate += 1
                    if richiede_apertura(r, archivio, soglia, salta_lavorate,
                                         contatore.zona):
                        da_aprire += 1
            return accettate, da_aprire

        lista_esaurita = False
        # Fin dove si era arrivati a scorrere, in pixel: la misura di
        # riferimento per accorgersi che la lista e' ripartita dalla cima.
        ultimo_top: int | None = None

        while True:
            esito_stop = await _deve_fermarsi()
            if esito_stop == "halted":
                raise BotHaltedError("kill-switch")
            if esito_stop is not None:
                return None
            if campaign.list_target and already >= campaign.list_target:
                logger.info(f"[InboxBrowser] Target {campaign.list_target} raggiunto ({already})")
                break

            # La lista e' ripartita da capo mentre lavoravamo? Il controllo e'
            # di sole letture del DOM (nessuna richiesta verso Instagram) e la
            # posizione precedente e' quella misurata alla fine del giro
            # scorso. Senza questo, un reset lasciava il motore a riattraversare
            # tutta la lista dall'inizio un gesto alla volta — e' il
            # "ricomincia da capo" riportato dal vivo.
            stato_ora = await stato_lista(page)
            if lista_ripartita_da_capo(ultimo_top, stato_ora.top):
                logger.warning(
                    f"[InboxBrowser] lista ripartita da capo: ero a {ultimo_top}px, "
                    f"ora sono a {stato_ora.top}px (navigazioni spontanee finora: "
                    f"{len(navigazioni)}) — risalgo con i lanci"
                )
                emit_event(campaign_id, "scrape_batch",
                           "La lista dei messaggi e' ripartita dalla cima: "
                           "recupero la posizione senza rileggere tutto", level="warn")
                obiettivo = ultimo_top or 0
                for _ in range(MAX_LANCI_RECUPERO):
                    stato_ora = await lancia(page)
                    if stato_ora.top is None or stato_ora.top >= obiettivo * 0.9:
                        break
                    if stato_ora.al_fondo:
                        break
            ultimo_top = stato_ora.top if stato_ora.top is not None else ultimo_top

            if not righe_del_giro:
                await raccogli(await leggi_righe_visibili(page, LINGUA))
            righe, righe_del_giro = righe_del_giro, []

            righe_incontrate = 0
            righe_saltate_segnalibro = 0

            for riga in righe:
                esito_stop = await _deve_fermarsi()
                if esito_stop == "halted":
                    raise BotHaltedError("kill-switch")
                if esito_stop is not None:
                    return None
                if riga.non_letta:
                    # Preserva il badge dei non letti (decisione di Tommaso): si
                    # salta SENZA aprire e senza contare nel riconoscimento, la
                    # riga resta da raccogliere quando sarà stata letta a mano.
                    # Non entra nella memoria di sessione: se nel frattempo
                    # viene letta a mano va rivalutata, e il salto qui non paga
                    # comunque nessuna pausa da risparmiare.
                    continue

                chiave = normalizza_nome(riga.nome)
                if gia_esaminata(chiave, viste_in_sessione):
                    # Gia' guardata in questa sessione: niente pausa, niente
                    # decisione, niente contatore di zona. Ripagarla e' il 65%
                    # delle pause di scorrimento misurate l'11/08.
                    continue
                if chiave:
                    viste_in_sessione.add(chiave)

                righe_incontrate += 1
                analizzata = analizza_riga_lista(riga.testo_grezzo, LINGUA)
                if riga_da_saltare(analizzata.data_relativa, soglia, salta_lavorate):
                    # Zona gia' lavorata: si attraversa e basta, niente
                    # decisione, niente apertura, niente contatore di zona.
                    righe_saltate_segnalibro += 1
                    continue

                riconosciuta_prima = archivio.e_riconosciuto(riga.nome)
                # Stessa funzione che ha deciso se fermare il gesto: se qui si
                # decidesse diversamente, il motore si fermerebbe dove non
                # serve o passerebbe oltre proprio dove serviva.
                ha_aperto = richiede_apertura(riga, archivio, soglia,
                                              salta_lavorate, contatore.zona)
                if ha_aperto:
                    username = await apri_riga(page, riga.indice, riga.nome, LINGUA, account.username)
                    if username:
                        testo_pagina = await page.evaluate("() => document.body.innerText")
                        dati = DatiContatto(
                            username=username,
                            nome=riga.nome,
                            last_message_at=estrai_data_thread(testo_pagina, LINGUA),
                            last_message_from=(
                                "us" if riga.ultimo_nostro is True
                                else "them" if riga.ultimo_nostro is False
                                else None
                            ),
                            last_message_text=estrai_ultimo_messaggio(testo_pagina, LINGUA),
                        )
                        esito = await salva_contatto(db, campaign_id, dati)
                        archivio.aggiungi(riga.nome)
                        # Il cursore segna quanto in basso si e' arrivati:
                        # avanza per OGNI riga davvero lavorata (aperta e
                        # verificata), non solo per i contatti nuovi — anche
                        # un contatto gia' noto ma appena riaggiornato e'
                        # comunque un punto vero della lista attraversato.
                        # Prima la data ASSOLUTA del thread appena aperto (gia'
                        # in mano, e leggibile anche sulle chat vecchie, dove
                        # l'eta' relativa della riga non lo e' — vedi
                        # nuovo_cursore_da_data). L'eta' della riga resta come
                        # ripiego per quando il thread non l'ha data.
                        if dati.last_message_at is not None:
                            campaign.inbox_cursor_at = nuovo_cursore_da_data(
                                campaign.inbox_cursor_at, dati.last_message_at)
                        else:
                            eta = eta_riga_in_ore(analizzata.data_relativa, LINGUA)
                            if eta is None:
                                logger.debug(
                                    f"[InboxBrowser] cursore fermo: ne' data del "
                                    f"thread ne' eta' della riga leggibili per "
                                    f"{riga.nome!r} (riga: {analizzata.data_relativa!r})"
                                )
                            campaign.inbox_cursor_at = nuovo_cursore(
                                campaign.inbox_cursor_at, eta, datetime.utcnow())
                        campaign.inbox_cursor_updated_at = datetime.utcnow()
                        if esito == "creato":
                            already += 1
                            nuovi_in_sessione += 1
                            since_break += 1
                            campaign.total_followers = already
                            campaign.updated_at = datetime.utcnow()
                        # Commit SEMPRE, non solo su 'creato'. La sessione ha
                        # autoflush di default, quindi il prossimo
                        # `_deve_fermarsi()` (db.refresh su ogni riga/giro)
                        # avrebbe comunque flushato il cursore prima di
                        # rileggerlo — verificato con un test dedicato che
                        # esercita esattamente questo percorso (esito
                        # 'aggiornato', nessun altro punto di commit prima
                        # della fine sessione): il valore sopravvive anche
                        # senza questo commit esplicito. Lo si fa comunque per
                        # non dipendere da quel dettaglio implicito — un
                        # domani con `autoflush=False` la stessa modifica
                        # sparirebbe in silenzio.
                        await db.commit()
                        if esito == "creato":
                            emit_event(campaign_id, "scrape_batch",
                                       f"Inbox: {already}" + (f"/{campaign.list_target}" if campaign.list_target else ""))
                    else:
                        # username None: verifica post-click fallita, profilo
                        # sparito, oppure — il caso di gran lunga piu' frequente
                        # (misurato) — riga uscita dal DOM prima del click.
                        # Nessuna scrittura, ma la riga va rimessa in gioco:
                        # toglierla dalla memoria di sessione la fa ricompare
                        # al prossimo campionamento che la incontra. Senza
                        # questo, un fallimento di apertura equivaleva a
                        # cancellare il contatto per tutta la sessione.
                        tentativi[chiave] = tentativi.get(chiave, 0) + 1
                        if chiave and tentativi[chiave] < MAX_TENTATIVI_RIGA:
                            viste_in_sessione.discard(chiave)
                        elif chiave:
                            logger.info(
                                f"[InboxBrowser] {riga.nome!r} non aperta dopo "
                                f"{MAX_TENTATIVI_RIGA} tentativi — lasciata alla "
                                f"prossima sessione"
                            )

                contatore.registra(riconosciuta_prima)
                # La pausa dipende da cosa si e' FATTO, non da dove ci si trova:
                # una riga solo scorsa non ha toccato Instagram, e pagarla al
                # ritmo delle aperture costava il 91% del tempo di sessione
                # (misurato l'11/08). Vedi ritmo.zona_pausa.
                await asyncio.sleep(campiona_pausa(zona_pausa(contatore.zona, ha_aperto)))

                durata_s = (datetime.utcnow() - session_started).total_seconds()
                if durata_s >= session_budget_s:
                    minutes = random.uniform(settings.inbox_break_min_minutes, settings.inbox_break_max_minutes)
                    seconds = int(minutes * 60)
                    campaign.scrape_break_prev_status = CampaignStatus.listing.value
                    campaign.status = CampaignStatus.listing_break
                    campaign.scrape_break_until = datetime.utcnow() + timedelta(seconds=seconds)
                    campaign.updated_at = datetime.utcnow()
                    await db.commit()
                    emit_event(campaign_id, "scrape_break", f"Pausa inbox {int(minutes)} min dopo {already}")
                    return seconds

            if lista_esaurita:
                # Ultimo giro gia' concesso dopo "fine" (vedi sotto): le righe
                # scoperte dal gesto che ha portato alla fine lista sono state
                # processate qui sopra. Nessun altro scroll da fare.
                drained = nuovi_in_sessione == 0
                break

            # Se in questo giro non si e' fatto altro che saltare (e ce n'era
            # almeno una da valutare), si sta attraversando la zona gia'
            # lavorata: si copre distanza con un lancio invece che con uno
            # scorrimento che campiona. La lettura qui non serve — per
            # definizione quelle righe non vanno aperte — quindi il gesto
            # puo' essere lungo. `righe_incontrate == 0` (niente di nuovo
            # trovato: tutto gia' in viste_in_sessione) non conta come "solo
            # saltate": ricade nel ramo normale sotto, che usa decidi_fine_lista
            # per accorgersi se la lista e' davvero finita o solo virtualizzata
            # oltre cio' che si e' gia' visto.
            solo_saltate = (
                salta_lavorate and righe_incontrate > 0
                and righe_saltate_segnalibro == righe_incontrate
            )
            if solo_saltate:
                lanci_a_vuoto += 1
                if lanci_a_vuoto >= 40:
                    logger.info(
                        f"[InboxBrowser] 40 lanci senza incontrare una riga da lavorare: "
                        f"la soglia del segnalibro ({soglia:.0f}h) copre tutta la lista"
                    )
                    segnalibro_esaurito = True
                    break
                await lancia(page)
                continue
            lanci_a_vuoto = 0

            # Esaurite le righe di questo giro: decidi_fine_lista fa lei scroll +
            # attese a pazienza crescente + la decisione finale (Task 8, gia'
            # corretta in review per contare i falliti nella FINESTRA di attesa,
            # non cumulativi di sessione — non reimplementare qui). Lo
            # scorrimento campiona le righe che attraversa (Task 4a) e le versa
            # in `raccogli`: il giro successivo le processa senza rileggere da
            # capo — e senza aver perso quelle di mezzo che il vecchio scroll
            # cieco lasciava fuori dal DOM virtualizzato.
            decisione = await decidi_fine_lista(page, falliti_inbox, LINGUA, raccogli)
            if decisione == "continua":
                continue
            if decisione == "piantato":
                raise ScraperError(
                    "Lista inbox piantata: richieste fallite verso gli endpoint inbox durante l'attesa"
                )
            # decisione == "fine": il gesto che l'ha decisa puo' aver campionato
            # righe nuove proprio mentre confermava che la lista non cresce piu'
            # (bug trovato dal QA: 29 contatti persi in un colpo solo perche' il
            # ciclo usciva subito senza mai riconsumare `righe_del_giro`). Si
            # concede un ultimo giro per processarle, POI si esce.
            lista_esaurita = True
            continue

        campaign.status = CampaignStatus.ready
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        if segnalibro_esaurito:
            emit_event(campaign_id, "scrape_complete",
                       "Modalita' segnalibro: nessuna chat piu' vecchia del segnalibro, "
                       "niente da raccogliere. Rilancia senza la spunta per rivedere "
                       "anche le chat piu' recenti.", level="warn")
        elif drained:
            emit_event(campaign_id, "scrape_complete",
                       f"Nessun contatto nuovo in questa sessione ({already} totali in lista) — "
                       "rilanciare non serve finché non arrivano nuovi DM in entrata.", level="warn")
        else:
            emit_event(campaign_id, "scrape_complete", f"Fase Lista inbox completata: {already} contatti in lista")
        return None

    except BotHaltedError:
        campaign.status = CampaignStatus.paused
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_stopped", "Bot in pausa globale — inbox interrotta", level="warn")
        return None
    except (ScrapeBudgetError, ScraperError) as e:
        campaign.status = CampaignStatus.error
        campaign.updated_at = datetime.utcnow()
        await db.commit()
        emit_event(campaign_id, "scrape_stopped", f"Fase Lista inbox (browser) non avviata: {e}", level="error")
        return None
    except Exception as e:
        if is_challenge_exception(e) and account is not None:
            await isolate_challenged_account(db, campaign, account, e)
        else:
            logger.exception(f"[InboxBrowser] Errore campaign {campaign_id}: {e}")
            campaign.status = CampaignStatus.error
            campaign.updated_at = datetime.utcnow()
            await db.commit()
        return None
    finally:
        if session is not None:
            await session.close()
