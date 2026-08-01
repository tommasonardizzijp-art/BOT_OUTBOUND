"""Invio di UN messaggio WhatsApp: apertura chat, guardie, invio.

Il POM (whatsapp_page.py) espone segnali e non decide; la politica sta
qui. Ogni funzione di questo modulo che decide "si invia / non si invia"
e' pura o quasi, perche' deve essere provabile senza browser: le tre volte
in cui M1 ha sbagliato una guardia, il difetto era nel giudizio, non nel
DOM.
"""
from dataclasses import dataclass

from loguru import logger

from app.browser.whatsapp_page import OpenResult

# Segnali del POM che significano "la chat 1:1 non esiste": colpa del dato,
# non nostra. Copiati alla lettera da whatsapp_page.open_chat /
# _apri_chat_da_risultati / _history_signal: se cambiano li', questo modulo
# smette di riconoscerli e cade nel ramo fail-closed (colpa nostra), che e'
# il fallimento giusto.
_SEGNALI_CHAT_INESISTENTE = (
    "nessuna-cronologia:nessun-messaggio-nel-pannello",
    "nessuna-cronologia:sezione-chat-vuota:nessuna-conversazione-esistente",
    "nessuna-cronologia:nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione",
)

# Segnali che dicono "la pagina non era nello stato che ci aspettavamo":
# infrastruttura nostra. Il contatto NON si tocca.
_SEGNALI_COLPA_NOSTRA = (
    "nessuna-cronologia:casella-ricerca-non-trovata",
    "nessuna-cronologia:ricerca-non-svuotata",
    "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio",
)

_SEGNALE_RICERCA_VUOTA = "nessuna-cronologia:nessun-risultato-di-ricerca"


@dataclass
class EsitoApertura:
    puo_inviare: bool
    esito_contatto: str | None   # 'skipped' | None (None = non si tocca lo stato)
    motivo: str
    colpa_nostra: bool           # True -> conta verso l'escalation FM2 del numero


def valuta_apertura(res: OpenResult) -> EsitoApertura:
    """Traduce (ok, signal) del POM nella decisione. Contratto sez. 3.1-3.2.

    Fail-closed su tutto cio' che non e' riconosciuto: un segnale nuovo
    (POM aggiornato, WhatsApp cambiato) non deve mai finire nel ramo che
    marca il contatto, perche' quello e' irreversibile per il contatto e
    invisibile a chi guarda i log.
    """
    signal = res.signal or ""

    if res.ok and signal.startswith("cronologia:"):
        try:
            n = int(signal.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            logger.error(f"valuta_apertura: conteggio illeggibile in {signal!r} -- "
                         "non si invia")
            return EsitoApertura(False, None, "segnale_illeggibile", True)
        if n >= 1:
            return EsitoApertura(True, None, f"cronologia:{n}", False)
        return EsitoApertura(False, None, "cronologia_vuota", True)

    if signal in _SEGNALI_CHAT_INESISTENTE:
        return EsitoApertura(False, "skipped", "no_existing_chat", False)

    if signal in _SEGNALI_COLPA_NOSTRA:
        return EsitoApertura(False, None, signal.split(":", 1)[1], True)

    if signal == _SEGNALE_RICERCA_VUOTA:
        # Ambiguo: lo scioglie il chiamante col contesto di sessione (sez. 3.3).
        return EsitoApertura(False, None, "ricerca_senza_risultati", False)

    logger.error(f"valuta_apertura: segnale non catalogato {signal!r} -- "
                 "trattato come guasto nostro, il contatto non si tocca")
    return EsitoApertura(False, None, "segnale_non_catalogato", True)


@dataclass
class EsitoGuardia:
    puo_inviare: bool
    motivo: str
    prova: str | None = None      # il testo dell'inbound che ha bloccato


async def guardia_pre_invio(pom, *, gia_scritto_prima: bool,
                            browser_avviato_da_s: float) -> EsitoGuardia:
    """Guardia opt-out/reply a chat APERTA. E' la garanzia strutturale del
    canale (SDD 7.2): con questa, uno STOP non e' mai scavalcabile, anche
    tra campagne distanti mesi e anche se lo scan lista lo ha perso.

    Costo misurato in M0: mediana 5,7 s, p95 7,5 s, max 12,1 s -- di cui la
    quasi totalita' e' il caricamento della cronologia, che e' PARTE della
    guardia e non e' aggirabile (la conversazione e' virtualizzata).

    Ordine dei controlli scelto per costo crescente: prima quelli che non
    toccano il DOM.
    """
    from app.config import settings
    from app.services import wa_optout

    # 1. Quarantena post-riconnessione (contratto sez. 3.4.2). Costo zero, e
    #    copre la finestra in cui QUALUNQUE lettura sarebbe inaffidabile.
    #    ATTENZIONE: 15 minuti e' un valore STIMATO, non misurato -- si
    #    rimisura quando il selettore SYNC_INDICATOR verra' catturato.
    quarantena_s = float(settings.wa_resync_quarantine_min) * 60
    if browser_avviato_da_s < quarantena_s:
        return EsitoGuardia(False, "quarantena_risync")

    # 2. Indicatore di sincronizzazione. Oggi torna sempre 'unknown' e
    #    'unknown' NON vale 'synced': semplicemente non e' un segnale.
    #    'syncing' invece blocca -- e blocchera' da solo il giorno in cui il
    #    selettore sara' catalogato, senza toccare questo codice.
    if await pom.sync_state() == "syncing":
        return EsitoGuardia(False, "sincronizzazione_in_corso")

    # 3. Caricare la cronologia FA PARTE della guardia: senza, nel DOM
    #    restano ~17 messaggi degli ultimi minuti e uno STOP di venti minuti
    #    prima non esiste (misurato in M0).
    info = await pom.load_history(minimo=int(settings.wa_guard_history_min))

    # 3b. Scroll mai tentato (box del pannello non trovato): 'after' e' solo
    #     cio' che c'era gia' nel DOM prima della chiamata, non uno storico
    #     validato -- stesso principio del punto 5, un segnale illeggibile
    #     non e' un segnale sicuro.
    if not info.ok:
        return EsitoGuardia(False, "storico_non_caricato")

    # 4. Incoerenza DB<->DOM: se avevamo gia' scritto a questo contatto e il
    #    DOM mostra zero messaggi, la chat non e' sincronizzata. Vale una
    #    query e chiude la falla piu' pericolosa che ci resta aperta.
    if gia_scritto_prima and info.after == 0:
        return EsitoGuardia(False, "incoerenza_db_dom")

    # 5. Coda inbound. None = CECITA' (nessuna bolla agganciata, o righe
    #    malformate): non e' silenzio, e non si invia. [] = silenzio vero.
    coda = await pom.read_inbound_tail(n=int(settings.wa_guard_tail_n))
    if coda is None:
        return EsitoGuardia(False, "coda_non_agganciata")

    for testo in coda:
        if wa_optout.looks_like_stop(testo):
            return EsitoGuardia(False, "optout", prova=testo[:300])

    # Una risposta qualsiasi ferma la sequenza (SDD 7.4, decisione 24/07),
    # ma NON e' questa funzione a marcarlo: qui si dice solo che c'e'.
    if coda:
        return EsitoGuardia(False, "ha_risposto", prova=coda[-1][:300])

    return EsitoGuardia(True, "silenzio")


def prepara_testo(step, contact, campaign) -> tuple[str, str]:
    """(testo pronto da digitare, variante 'a'..'d').

    Il rendering vero sta in wa_template.py, che e' di M2 (contratto sez. 2.4):
    qui si sceglie la variante, si rende, e si appende la CTA di opt-out --
    che il renderer NON deve conoscere, perche' e' una regola di campagna,
    non di template.
    """
    from app.services.wa_template import pick_wa_template, render_wa_template

    template, variante = pick_wa_template(step)
    testo = render_wa_template(
        template,
        display_name=getattr(contact, "display_name", None),
        attributes=getattr(contact, "attributes", None),
    )

    # CTA solo sul PRIMO messaggio della sequenza (SDD 7.2): ripeterla a
    # ogni step la trasforma in rumore, e l'obbligo ePrivacy riguarda il
    # primo contatto della campagna.
    if step.step_index == 0 and getattr(campaign, "optout_enabled", False):
        cta = (getattr(campaign, "optout_cta", None) or "").strip()
        if not cta:
            raise ValueError(
                "Campagna con optout_enabled=True e optout_cta vuota: non si "
                "manda marketing senza via d'uscita (contratto sez. 2.1)."
            )
        testo = f"{testo}\n\n{cta}"

    return testo, variante
