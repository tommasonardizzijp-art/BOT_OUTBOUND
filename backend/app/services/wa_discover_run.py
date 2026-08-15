# backend/app/services/wa_discover_run.py
"""Fase A auto-discover: l'orchestratore del giro di scansione.

Mette insieme i sei moduli di app/services/wa_discover/ (classifica,
sincronizzazione, sidebar, pannello, salvataggio) in un unico giro di
scansione per UN wa_number_id. Struttura ricalcata su
scrape_inbox_browser.run_inbox_browser_list (il gemello Instagram), con le
differenze imposte dal dominio (Task 7 del piano
docs/superpowers/plans/2026-08-11-wa-fase4-fase-a-autodiscover.md):

- NESSUNA campagna: si lavora su un number_id, non su una WaCampaign.
- Kill-switch WA: bot_state_service.is_wa_halted() -- NON is_halted(), che e'
  la colonna del canale Instagram (trappola gia' documentata, spec 2.2).
- Lock del profilo (wa_profile_lock) prima di aprire il browser, rinnovato
  durante il giro: il cron wa_session_healthcheck apre lo stesso profilo, e
  due Chromium sullo stesso user-data-dir lo corrompono (stesso rischio
  descritto nel modulo wa_profile_lock e gia' gestito da
  wa_worker.esegui_mini_sessione).
- Gate di sincronizzazione (sincronizzazione.puo_scansionare) prima di
  iniziare: sotto soglia non si scansiona affatto.
- SOLA LETTURA ASSOLUTA: nessuna quarantena d'invio, nessun cap, nessun
  warmup, nessun breaker/dead-man switch -- la Fase A non manda niente
  (spec 5.5). Se questo file inizia a toccare uno di quei meccanismi, e'
  fuori scope.
- Ritmo: inbox_browser.ritmo (zona_pausa/campiona_pausa), con la
  distinzione che conta -- azione (pannello aperto) vs scorrimento (riga
  risolta dal solo titolo, WhatsApp non l'ha vista) -- non "zona piena vs
  zona rapida": qui non esiste un archivio di nomi gia' noti fra un giro e
  l'altro come in ArchivioNomi (quello serve a Instagram per non riaprire
  DM gia' letti in cima a una lista che si ripresenta identica a ogni
  giro); ogni riga aperta in un giro di discover e' un'estrazione di dato
  nuovo, quindi si paga sempre il ritmo pieno ("piena") quando si apre.
- Fine giro: sidebar.totale_dichiarato(page) letto a inizio giro, confrontato
  col raccolto (salvate+aggiornate) a fine giro -- e' cosi' che ci si
  accorge di una raccolta parziale invece di festeggiarla come completata
  (su questo canale e' gia' successo il contrario: una campagna chiusa
  "completed" con zero messaggi inviati, vedi memoria
  botoutbound-wa-regola-v2-numero-vergine).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select

from app.services import bot_state_service, wa_profile_lock
from app.services.inbox_browser.ritmo import campiona_pausa, zona_pausa
from app.services.wa_discover import classifica, pannello, salvataggio, sidebar
from app.services.wa_discover.sincronizzazione import (
    SOGLIA_DEFAULT, leggi_percentuale, leggi_sincronizzazione,
    lista_utilizzabile, puo_scansionare, puo_scansionare_lettura,
)
from app.utils.events import emit as emit_event
from app.utils.phone_pseudonym import hmac_phone

# Quanti passi di scorrimento SENZA righe nuove (mai viste in questo giro)
# prima di arrendersi. La geometria qui e' piu' semplice che nel motore
# inbox Instagram (decidi_fine_lista, pazienza crescente su piu' tentativi):
# aria-rowcount dice gia' il totale atteso e StatoScorrimento.al_fondo dice
# il fondo reale, quindi basta un contatore piccolo per distinguere "la
# lista e' davvero finita prima del fondo" (raro) da "il contenitore non
# scorre piu'" (bug/selettore) -- in entrambi i casi non ha senso continuare
# a scorrere all'infinito.
MAX_SCROLL_SENZA_NUOVE_RIGHE = 3

# Scarto (frazione) fra il totale dichiarato da WhatsApp (aria-rowcount) e il
# totale raccolto (salvate+aggiornate) oltre il quale l'evento finale deve
# avvertire di una raccolta probabilmente parziale, invece di festeggiarla
# come completa. 20%: sotto quella soglia lo scarto e' spiegabile da rumore
# normale (righe non verificate ritentate al giro dopo, intestazioni di
# sezione filtrate); sopra, e' il segnale che qualcosa ha fermato lo scan
# prima del tempo (kill-switch, sidebar piantata, motore cambiato).
SCARTO_PARZIALE_SOGLIA = 0.20


@dataclass
class DecisioneRiga:
    """L'esito della decisione su UNA riga della sidebar.

    `riga` e' None quando la riga non e' salvabile (esito.salvabile falso
    in pannello.apri_e_leggi: si e' aperta un'altra persona, o la riga non
    era piu' nel DOM) -- in quel caso non si scrive niente, si ritenta al
    giro dopo. `ha_aperto` e' per il ritmo: dice se questa riga ha
    davvero toccato WhatsApp (pannello aperto, anche se poi la verifica e'
    fallita) o si e' risolta dal solo titolo, senza nessuna interazione.
    """
    riga: classifica.RigaScoperta | None
    ha_aperto: bool
    # Riga gia' presente in wa_discovered_chats con un numero: non si riapre
    # il pannello. Distinta da riga=None, che significa "provato e fallito".
    saltata: bool = False


async def _decidi_riga(page, grezza: dict, *,
                       titoli_noti: set[str] | None = None,
                       hmac_noti: set[str] | None = None) -> DecisioneRiga:
    """Da una riga grezza di sidebar.scan_sidebar (titolo, titolo_e_numero) a
    una RigaScoperta pronta per salva_scoperta, o a `None` se non si puo'
    scrivere nulla.

    Il 39% che non costa nulla (misurato PoC-5, Task 5): se `titolo_e_numero`
    e' vero il titolo E' gia' il numero -- si estrae senza aprire il
    pannello, il risparmio principale sui 5,3s medi che apri_e_leggi costa.
    Il titolo GREZZO (non None) passa comunque a RigaScoperta: e'
    salva_scoperta/etichetta_visibile a decidere se e come mascherarlo
    (P12), non questa funzione -- passare gia' None qui butterebbe via
    l'etichetta mascherata che l'operatore di Fase B vedrebbe altrimenti
    (vedi classifica.etichetta_visibile e il test
    test_il_nome_vero_sostituisce_l_etichetta_mascherata).
    """
    titoli_noti = titoli_noti or set()
    hmac_noti = hmac_noti or set()
    titolo = grezza.get("titolo")

    # Riscansione incrementale: una chat gia' in staging con il suo numero non
    # si ripaga. Il costo di uno scan non e' nello scorrere, e' nel CLICCARE
    # ogni riga per aprire il pannello -- 12 secondi l'una, misurati il 14/08
    # su PRIMERO MAGAZZINO (78 chat in 16 minuti). Su 900 chat sono ore, e il
    # discover periodico diventerebbe impraticabile.
    #
    # LA CHIAVE E' L'HMAC, NON IL TITOLO. Provato sul campo il 14/08 e
    # fallito: su 241 righe in staging, 194 hanno il titolo MASCHERATO
    # (`+39•••••761`, vincolo P12) perche' il titolo E' il numero, mentre dal
    # DOM quel titolo arriva in chiaro. Confrontando i titoli il salto scatta
    # su 47 righe su 241 invece che su 232 -- cioe' quasi mai, proprio dove
    # servirebbe di piu'. Il titolo resta il ripiego per le chat con un nome
    # vero, che un numero nel titolo non ce l'hanno.
    #
    # Conseguenza dichiarata: un contatto che cambia numero mantenendo lo
    # stesso nome non viene riverificato. Per lo scopo -- trovare chi ci ha
    # scritto di nuovo -- e' accettabile.
    if titolo and titolo in titoli_noti:
        return DecisioneRiga(riga=None, ha_aperto=False, saltata=True)
    if grezza.get("titolo_e_numero"):
        numero_dal_titolo = classifica.numero_dal_titolo(titolo)
        if numero_dal_titolo is not None and hmac_phone(numero_dal_titolo) in hmac_noti:
            return DecisioneRiga(riga=None, ha_aperto=False, saltata=True)

    if grezza.get("titolo_e_numero"):
        numero = classifica.numero_dal_titolo(titolo)
        numero_leggibile = numero is not None
        tipo = classifica.tipo_da_segnali(
            numero_leggibile=numero_leggibile, testo_pannello=None, titolo=titolo)
        return DecisioneRiga(
            riga=classifica.RigaScoperta(titolo=titolo, numero=numero,
                                         numero_leggibile=numero_leggibile, tipo=tipo),
            ha_aperto=False,
        )

    esito = await pannello.apri_e_leggi(page, titolo)
    # riga_assente: la riga non era piu' nel DOM, NESSUN click e' partito --
    # WhatsApp non l'ha vista, e per il ritmo conta come scorrimento, non
    # come azione (a differenza di non_verificata, dove il click e' successo
    # ma su un'altra persona: quello WhatsApp lo ha visto).
    ha_aperto = esito.esito != pannello.ESITO_RIGA_ASSENTE
    if not esito.salvabile:
        return DecisioneRiga(riga=None, ha_aperto=ha_aperto)

    numero_leggibile = esito.numero is not None
    tipo = classifica.tipo_da_segnali(
        numero_leggibile=numero_leggibile, testo_pannello=esito.testo_pannello,
        titolo=titolo)
    return DecisioneRiga(
        riga=classifica.RigaScoperta(titolo=titolo, numero=esito.numero,
                                     numero_leggibile=numero_leggibile, tipo=tipo),
        ha_aperto=True,
    )


async def _esegui_scan(page, *, db, tenant_id: str, number_id: str,
                       lock_token: str | None = None,
                       soglia_sync: int = SOGLIA_DEFAULT) -> dict:
    """Il giro di scan vero e proprio, su una `page` gia' aperta e loggata.

    NON apre il browser, NON prende il lock del profilo, NON naviga: quello
    e' compito del chiamante (esegui_discover_run). Separata apposta cosi'
    si testa con una pagina finta (sul modello di test_wa_discover_sidebar.py),
    senza montare un profilo Chromium reale.

    `lock_token`, se dato, fa rinnovare il lucchetto del profilo a ogni riga
    processata (stesso principio di wa_worker.esegui_mini_sessione: la
    scadenza del TTL deve dipendere dall'ultimo segno di vita, non dalla
    durata prevista del giro). None nei test che non montano un lock vero.
    """
    esito = {
        "salvate": 0, "aggiornate": 0, "saltate_gia_note": 0,
        "non_verificate": 0, "dichiarato": None, "motivo": "completato",
        "sync_stato": "ignota", "sync_letta": None,
    }

    # Gate di sincronizzazione, tri-stato (Task 7). La percentuale NON sta nel
    # body della pagina: vive dentro Impostazioni, e va aperto per leggerla
    # (verificato nel PoC-5 -- il censimento della sola sidebar non trovo'
    # nessuna percentuale, comparve solo dopo il click). Leggere
    # document.body.innerText senza aprire nulla non trova mai niente.
    #
    # Una lettura sola, nessun ritentativo: il 15/08 e' stato verificato dal
    # vivo che _SEL_IMPOSTAZIONI non matcha affatto su questo WhatsApp Web
    # (due sessioni distinte, "voce Impostazioni non trovata"). Con un
    # selettore sbagliato riprovare a 5s e 15s non cambia l'esito -- costa
    # venti secondi a ogni scansione per riottenere lo stesso "ignota".
    lettura = await leggi_sincronizzazione(page)

    esito["sync_stato"] = lettura.stato
    esito["sync_letta"] = lettura.percentuale
    ok_sync, motivo_sync = puo_scansionare_lettura(lettura, soglia=soglia_sync)
    if not ok_sync:
        logger.info(f"[WaDiscover] {number_id}: scan non avviato -- {motivo_sync}")
        emit_event(number_id, "wa_discover_skipped", motivo_sync, level="warn")
        esito["motivo"] = "sync_sotto_soglia"
        return esito
    logger.info(f"[WaDiscover] {number_id}: gate sync ok -- {motivo_sync}")

    # SECONDA RETE, dopo il collaudo fallito dell'11/08: si scansiona solo con
    # la sidebar scoperta. Un pannello rimasto aperto (Impostazioni, info
    # contatto) riduce le righe esposte da 65 a 5 e impedisce a qualunque click
    # di aprire una chat -- e non solleva nessun errore: il giro finisce "dopo
    # stallo" con una manciata di chat su 291, che sembra un problema di
    # scorrimento e invece e' una tenda davanti alla lista. Il gate qui sopra ha
    # gia' il dovere di richiudere cio' che apre; questo controllo esiste perche'
    # quel dovere puo' fallire, e fallire in silenzio e' il modo peggiore.
    if not await lista_utilizzabile(page):
        motivo = ("un pannello e' rimasto aperto sopra la lista chat: con la "
                  "sidebar coperta lo scan vedrebbe una frazione delle chat e la "
                  "dichiarerebbe completa")
        logger.error(f"[WaDiscover] {number_id}: scan non avviato -- {motivo}")
        emit_event(number_id, "wa_discover_skipped", motivo, level="error")
        esito["motivo"] = "sidebar_coperta"
        return esito

    # Il totale dichiarato si legge PRIMA di scorrere qualunque cosa: e' il
    # termine di paragone con cui, a fine giro, ci si accorge di una
    # raccolta parziale invece di chiamarla "completata".
    esito["dichiarato"] = await sidebar.totale_dichiarato(page)

    # Righe gia' in staging CON un numero: quelle senza vanno riprovate, e'
    # proprio il caso in cui il pannello non era arrivato.
    #
    # Si tengono DUE insiemi perche' le due chiavi coprono popolazioni
    # diverse: l'hmac copre le chat il cui titolo e' il numero (194 su 241
    # misurate il 14/08, e il loro chat_title a DB e' mascherato quindi
    # inconfrontabile), il titolo copre quelle con un nome vero (47 su 241).
    # Un titolo mascherato non entra nell'insieme dei titoli: non
    # combacerebbe mai con quello che arriva dal DOM.
    from app.models.wa import WaDiscoveredChat
    noti = await db.execute(
        select(WaDiscoveredChat.chat_title, WaDiscoveredChat.phone_hmac).where(
            WaDiscoveredChat.number_id == number_id,
            WaDiscoveredChat.phone_hmac.is_not(None)))
    coppie = noti.all()
    hmac_noti = {h for _, h in coppie}
    titoli_noti = {t for t, _ in coppie if t and "•" not in t}
    if hmac_noti:
        logger.info(f"[WaDiscover] {number_id}: {len(hmac_noti)} chat gia' note "
                    f"col numero ({len(titoli_noti)} anche per titolo), "
                    "non verranno riaperte")

    titoli_processati: set[str] = set()
    scroll_senza_nuove = 0

    while True:
        if await bot_state_service.is_wa_halted():
            esito["motivo"] = "wa_halted"
            break

        righe = await sidebar.scan_sidebar(page)
        nuove_in_questo_passo = 0
        for grezza in righe:
            # Kill-switch ricontrollato DENTRO il for, non solo in cima al
            # while: con pause che arrivano fino a diversi minuti per riga
            # e decine di righe per passo, controllare solo in cima al while
            # farebbe impiegare minuti prima di fermare davvero il browser.
            if await bot_state_service.is_wa_halted():
                esito["motivo"] = "wa_halted"
                break

            titolo = (grezza.get("titolo") or "").strip()
            if not titolo or titolo in titoli_processati:
                # Gia' vista in questo giro (overlap fra due passi di
                # scorrimento sulla lista virtualizzata) o senza titolo
                # (dovrebbe gia' essere filtrata da righe_dalla_sidebar, ma
                # una riga senza identita' non e' comunque processabile).
                continue
            titoli_processati.add(titolo)
            nuove_in_questo_passo += 1

            decisione = await _decidi_riga(page, grezza, titoli_noti=titoli_noti,
                                           hmac_noti=hmac_noti)
            if decisione.saltata:
                esito["saltate_gia_note"] += 1
            elif decisione.riga is not None:
                stato_salv = await salvataggio.salva_scoperta(
                    db, tenant_id, number_id, decisione.riga)
                if stato_salv == "creata":
                    esito["salvate"] += 1
                else:
                    esito["aggiornate"] += 1
            else:
                esito["non_verificate"] += 1

            if lock_token is not None:
                await wa_profile_lock.renew(number_id, lock_token)

            # La pausa dipende da cosa si e' FATTO, non da dove ci si trova:
            # una riga risolta dal solo titolo non ha toccato WhatsApp e non
            # merita la pausa di un'apertura (stesso principio di
            # inbox_browser.ritmo, misurato l'11/08 sul motore Instagram:
            # pagare tutto al ritmo delle aperture costava il 91% del tempo
            # di sessione). Sempre "piena" quando si apre: a differenza di
            # Instagram non esiste qui un archivio di nomi gia' noti da un
            # giro all'altro che giustifichi una zona "rapida".
            #
            # Le righe saltate non hanno toccato WhatsApp: nessuna pausa da
            # pagare. E' quello che rende rapido il ritorno al punto dove il
            # giro precedente si era fermato, invece di ricamminare la lista
            # a 0,3 secondi per riga.
            if not decisione.saltata:
                await asyncio.sleep(campiona_pausa(zona_pausa("piena", decisione.ha_aperto)))

        if esito["motivo"] == "wa_halted":
            break

        scroll_senza_nuove = 0 if nuove_in_questo_passo else scroll_senza_nuove + 1
        stato_scroll = await sidebar.scorri_sidebar(page)
        if stato_scroll.al_fondo:
            break
        if scroll_senza_nuove >= MAX_SCROLL_SENZA_NUOVE_RIGHE:
            if esito["motivo"] == "completato":
                esito["motivo"] = "fermato_dopo_stallo"
            logger.warning(
                f"[WaDiscover] {number_id}: {MAX_SCROLL_SENZA_NUOVE_RIGHE} scorrimenti "
                "consecutivi senza righe nuove, fondo lista mai raggiunto -- "
                "interrotto per non scorrere all'infinito su una sidebar piantata")
            break

    raccolte = esito["salvate"] + esito["aggiornate"] + esito["saltate_gia_note"]
    dettaglio = (f"{raccolte} chat coperte ({esito['salvate']} nuove, "
                f"{esito['aggiornate']} aggiornate, {esito['saltate_gia_note']} "
                f"gia' note), {esito['non_verificate']} righe non verificate "
                "(si ritentano al giro dopo)")

    if esito["motivo"] == "wa_halted":
        emit_event(number_id, "wa_discover_stopped",
                   f"kill-switch durante la scansione -- {dettaglio}", level="warn")
    elif esito["dichiarato"] is not None and esito["dichiarato"] > 0:
        scarto = (esito["dichiarato"] - raccolte) / esito["dichiarato"]
        if scarto >= SCARTO_PARZIALE_SOGLIA:
            if esito["motivo"] == "completato":
                esito["motivo"] = "raccolta_parziale"
            emit_event(number_id, "wa_discover_parziale",
                       f"{dettaglio} -- dichiarate {esito['dichiarato']}, scarto "
                       f"{round(scarto * 100)}%: raccolta probabilmente incompleta, "
                       "non e' una scansione completata", level="warn")
        else:
            emit_event(number_id, "wa_discover_complete",
                       f"{dettaglio} (dichiarate {esito['dichiarato']})")
    else:
        emit_event(number_id, "wa_discover_complete",
                   f"{dettaglio} (totale dichiarato non disponibile dal pannello)")

    logger.info(f"[WaDiscover] {number_id}: giro terminato -- {esito}")
    return esito


async def esegui_discover_run(number_id: str, *, soglia_sync: int = SOGLIA_DEFAULT,
                              headless: bool = True) -> dict:
    """Un giro di scansione Fase A per UN numero WhatsApp gia' onboardato.

    Governo del profilo ricalcato su wa_worker.esegui_mini_sessione (lock,
    kill-switch, apertura browser), ma senza campagna, senza quarantena e
    senza nessuno dei cancelli d'invio: qui si legge soltanto. Ritorna lo
    stesso dizionario di contatori di _esegui_scan, con `motivo` esteso ai
    casi che riguardano SOLO l'apertura del browser (numero non attivo,
    profilo occupato).
    """
    from app.database import AsyncSessionLocal
    from app.models.wa import WaNumber, WaNumberStatus

    esito = {
        "salvate": 0, "aggiornate": 0, "non_verificate": 0,
        "dichiarato": None, "motivo": "completato",
    }

    # Cancello 0, prima di toccare qualunque cosa (stesso ordine di
    # esegui_mini_sessione): il kill-switch e' la colonna WA
    # (is_wa_halted), MAI is_halted -- quella e' Instagram (trappola gia'
    # documentata, spec 2.2).
    if await bot_state_service.is_wa_halted():
        esito["motivo"] = "wa_halted"
        return esito

    async with AsyncSessionLocal() as db:
        number = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if number is None or number.status != WaNumberStatus.active:
            esito["motivo"] = "numero_non_attivo"
            return esito
        tenant_id, proxy_url = number.tenant_id, number.proxy_url

    try:
        async with wa_profile_lock.held(number_id) as lock_token:
            from app.browser.whatsapp_page import WhatsAppWebPage
            from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser

            async with _open_wa_browser(number_id, headless=headless,
                                        proxy_url=proxy_url) as context:
                page = await context.new_page()
                # `commit` e non `domcontentloaded`, con timeout largo: misurato
                # l'11/08 col PoC-5, il browser IN FINESTRA impiega 5-10s prima
                # che la UI sia usabile e il goto con domcontentloaded a 30s
                # (il default) FALLISCE. WhatsApp Web continua a caricare
                # risorse ben oltre il primo rendering: si aspetta la comparsa
                # della UI vera, con pazienza crescente, non un evento del
                # documento.
                await page.goto(WHATSAPP_WEB_URL, wait_until="commit", timeout=120_000)
                for attesa_s in (3, 5, 10, 20, 30):
                    await page.wait_for_timeout(attesa_s * 1000)
                    pronta = await page.evaluate(
                        "() => !!(document.querySelector('#pane-side') "
                        "|| document.querySelector('canvas'))"
                    )
                    if pronta:
                        break

                pom = WhatsAppWebPage(page)
                segnale = await pom.session_state()
                if segnale != "logged_in":
                    logger.warning(
                        f"[WaDiscover] {number_id}: sessione non loggata (segnale="
                        f"{segnale!r}) -- nessun tentativo di login automatico")
                    esito["motivo"] = "sessione_non_loggata"
                    return esito

                async with AsyncSessionLocal() as db:
                    esito = await _esegui_scan(
                        page, db=db, tenant_id=tenant_id, number_id=number_id,
                        lock_token=lock_token, soglia_sync=soglia_sync)
    except wa_profile_lock.WaProfileBusy:
        esito["motivo"] = "profilo_occupato"
        logger.info(f"[WaDiscover] {number_id}: profilo occupato (health-check o "
                    "invio in corso), giro saltato")
    except Exception as exc:
        # Sola lettura assoluta (vincolo del piano): un'eccezione imprevista
        # qui non deve mai lasciare lo stato del numero incoerente, ma
        # nemmeno propagare fino al chiamante (cron/job). Si logga e si
        # ritenta al prossimo giro schedulato -- nessuna scrittura su
        # WaNumber, a differenza di wa_worker che su un guasto nostro ferma
        # il numero: qui non c'e' nessun invio da proteggere.
        logger.exception(f"[WaDiscover] {number_id}: errore imprevisto durante il giro: {exc}")
        esito["motivo"] = "errore_imprevisto"

    logger.info(f"[WaDiscover] giro {number_id}: {esito}")
    return esito
