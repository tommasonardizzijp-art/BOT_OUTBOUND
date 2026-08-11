"""Il gate della Fase A: quanto e' sincronizzata la sessione WhatsApp Web.

Idea di Tommaso (11/08), e non e' un'alternativa alla quarantena esistente: e'
la MISURA DIRETTA di cio' che la quarantena stima a occhio. Oggi
_attendi_quarantena_risync (wa_worker.py) aspetta WA_RESYNC_QUARANTINE_MIN
minuti a browser aperto e fermo, per ogni mini-sessione, e la motivazione scritta
nel codice e' proprio la sincronizzazione ("finche' non ha finito la guardia
opt-out leggerebbe il vuoto invece del silenzio"). Un timer cieco sbaglia in
entrambe le direzioni.

ATTENZIONE ALLO SCOPE. Questo modulo governa lo SCAN, non l'invio. Per lo scan
una soglia bassa costa una raccolta parziale (recuperabile: si riscansiona). Per
l'invio costerebbe un messaggio a chi ha risposto STOP senza che noi lo avessimo
ancora sincronizzato.

L'ORDINE DI SINCRONIZZAZIONE E' NOTO, e questo restringe molto quel rischio.
Testo letterale del pannello, misurato l'11/08 col numero personale:

    "Sincronizzazione dei messaggi precedenti in corso"
    "Completata al 61%"   ->   "Completata all'87%" novanta secondi dopo

"Messaggi PRECEDENTI": WhatsApp scarica all'indietro, i recenti ci sono gia'.
Quindi un opt-out recente e' sincronizzato molto prima del 100%, e la soglia
bassa proposta da Tommaso e' difendibile anche per l'invio. Riserva residua, piu'
stretta ma reale: se il numero e' rimasto DISCONNESSO per giorni, gli opt-out di
quel periodo sono "precedenti" anche loro. Chi tocchera' la quarantena d'invio
deve tenerne conto -- p.es. soglia piu' alta quando session_checked_at e' vecchio.
"""
from __future__ import annotations

import re

from loguru import logger

# Una percentuale plausibile: 0-100. Il filtro sul range non e' pedanteria --
# 'IT01879020517A2026%' e' un nome di file vero visto nella sidebar, e senza
# limite superiore diventerebbe una sincronizzazione al 2026%.
_PERCENTUALE = re.compile(r"(?<!\d)(\d{1,3})\s*%")
# Le due lingue: il censimento dell'inbox Instagram ha trovato l'interfaccia in
# inglese su un account trattato come italiano, quindi la lingua non si assume.
_MARCATORE_SYNC = re.compile(r"sincronizzaz|syncing|sync", re.IGNORECASE)
_MARCATORE_COMPLETAMENTO = re.compile(r"complet", re.IGNORECASE)

SOGLIA_DEFAULT = 60


def _percentuale_in(testo: str | None) -> int | None:
    for grezzo in _PERCENTUALE.findall(testo or ""):
        valore = int(grezzo)
        if 0 <= valore <= 100:
            return valore
    return None


def percentuale_da_testi(testi: list[str] | None) -> int | None:
    """La percentuale di sincronizzazione, o None se il pannello non la espone.

    None NON e' zero: la percentuale sparisce anche quando la sincronizzazione
    e' finita. Confondere le due cose bloccherebbe la Fase A nel caso normale.

    LA PERCENTUALE SI LEGGE SOLO IN CONTESTO. Prendere la prima "N%" che capita
    e' pericoloso, e non per un caso di scuola: questi testi arrivano dal DOM, e
    il DOM di WhatsApp Web contiene le anteprime dei messaggi. Un cliente con in
    chat "sconto 50%" farebbe leggere al gate una sincronizzazione al 50% --
    sotto soglia, quindi la Fase A non partirebbe MAI su quel numero, con un
    comportamento diverso da cliente a cliente e nessun segnale visibile.

    Quindi: prima si verifica che nel pannello si stia davvero parlando di
    sincronizzazione, poi si cerca la percentuale vicino a quel discorso.
    """
    righe = [t or "" for t in testi or []]
    indici_sync = [i for i, t in enumerate(righe) if _MARCATORE_SYNC.search(t)]
    if not indici_sync:
        # Nessuno parla di sincronizzazione: qualunque percentuale qui dentro
        # appartiene a qualcos'altro (un messaggio, un nome di file).
        return None

    # 1) La percentuale nella riga stessa del marcatore ("Syncing... 23%").
    for i in indici_sync:
        valore = _percentuale_in(righe[i])
        if valore is not None:
            return valore

    # 2) Una riga di completamento ("Completata al 61%", "23% complete"): e' la
    #    forma vera misurata l'11/08, dove il marcatore e la percentuale stanno
    #    in due nodi separati.
    for t in righe:
        if _MARCATORE_COMPLETAMENTO.search(t):
            valore = _percentuale_in(t)
            if valore is not None:
                return valore

    # 3) Ultima spiaggia: la riga subito dopo il marcatore. Se WhatsApp cambia
    #    le parole ma non la struttura, questo continua a funzionare.
    for i in indici_sync:
        if i + 1 < len(righe):
            valore = _percentuale_in(righe[i + 1])
            if valore is not None:
                return valore
    return None


# Il pannello con la percentuale sta DENTRO Impostazioni: nella sidebar nuda
# non c'e' (verificato nel PoC-5 -- il primo censimento, che guardava solo cio'
# che era gia' visibile, non trovo' nessuna percentuale; comparve solo dopo il
# click su Impostazioni). Leggere document.body.innerText senza aprire nulla
# quindi non trova mai niente: il gate direbbe sempre "non lo so" e lascerebbe
# sempre passare, cioe' non sarebbe un gate.
_SEL_IMPOSTAZIONI = "[aria-label='Impostazioni'], [aria-label='Settings']"
_JS_TESTI_PAGINA = """() => [...document.querySelectorAll('span, div, p')]
    .filter(e => e.children.length === 0)
    .map(e => (e.textContent || '').trim())
    .filter(t => t && t.length < 200)"""


async def leggi_percentuale(page) -> int | None:
    """Apre Impostazioni, legge la percentuale di sincronizzazione, richiude.

    Sola lettura: aprire un pannello non modifica niente. Se Impostazioni non
    si apre (o non espone la percentuale) si ritorna None, che `puo_scansionare`
    tratta come "non lo so" e non come "fermo" -- ma il chiamante deve sapere
    che quel None puo' voler dire tre cose diverse: sincronizzazione finita,
    pannello non trovato, oppure UI cambiata. Il motivo restituito dal gate lo
    dichiara, ed e' il primo posto da guardare se una raccolta esce corta.
    """
    try:
        voce = page.locator(_SEL_IMPOSTAZIONI).first
        if not await voce.count():
            logger.warning(
                "[WaDiscover] voce Impostazioni non trovata: percentuale di "
                "sincronizzazione non leggibile"
            )
            return None
        await voce.click(timeout=4000)
        await page.wait_for_timeout(1500)
        testi = await page.evaluate(_JS_TESTI_PAGINA)
        percentuale = percentuale_da_testi(testi)
        return percentuale
    except Exception as exc:
        logger.warning(f"[WaDiscover] lettura sincronizzazione fallita: {exc}")
        return None
    finally:
        # Si richiude sempre, anche se la lettura e' fallita: lasciare
        # Impostazioni aperto coprirebbe la lista chat per tutto il giro.
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
        except Exception:
            pass


def puo_scansionare(percentuale: int | None, soglia: int = SOGLIA_DEFAULT) -> tuple[bool, str]:
    """(si_parte, motivo). Il motivo finisce nei log e negli eventi: e' il primo
    posto dove si guarda quando una raccolta risulta piu' corta del previsto."""
    if percentuale is None:
        return True, ("percentuale di sincronizzazione non esposta dal pannello: "
                      "si procede, ma se la raccolta risulta corta e' il primo indiziato")
    if percentuale >= soglia:
        return True, f"sincronizzazione al {percentuale}% (soglia {soglia}%)"
    return False, (f"sincronizzazione al {percentuale}%, sotto la soglia del {soglia}%: "
                   "scansionare ora raccoglierebbe una parte delle chat e la "
                   "dichiarerebbe completa")
