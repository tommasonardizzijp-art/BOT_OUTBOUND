"""WhatsApp Web Page Object Model. Eredita il codice del PoC di M0
(scripts/poc_wa/), non riparte da zero: ogni scelta qui sotto e' la
correzione di un errore gia' commesso e gia' pagato in quattordici giorni di
misura reale (SDD-whatsapp-channel.md, sez. 6.4).

Il POM non decide se inviare. Espone segnali (OpenResult, HistoryInfo,
read_inbound_tail, sync_state, ChatRow); la politica -- guardia opt-out, cap,
opt-out persistito -- sta in wa_sender, che e' M3. Un POM che decide e' un
POM che non si puo' testare senza browser.

In M1 send_text esiste per completare l'interfaccia ma non va esercitato
contro WhatsApp vero: gli invii sono M3, dopo cap e guardie.
"""
import asyncio
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from loguru import logger

from app.browser import human_input
from app.browser import whatsapp_selectors as sel
from app.utils.phone_pseudonym import mask_phone


@dataclass
class OpenResult:
    ok: bool
    ms: float
    signal: str
    # Badge "non letto" della riga nei risultati di ricerca, letto PRIMA del
    # click che apre la chat (e la marca letta). Serve a chi deve aprire una
    # chat per un'operazione una tantum (es. backfill 21/08) e vuole poter
    # dire quali erano davvero da leggere, senza dover indovinare un
    # selettore nuovo per il "segna come da leggere" del menu contestuale
    # (mai verificato dal vivo, troppo vicino a 'Elimina'/'Archivia' per
    # rischiarlo su chat vere senza una POC dedicata prima). Default False:
    # ogni chiamante esistente che non legge questo campo non cambia
    # comportamento.
    era_non_letto: bool = False


@dataclass
class HistoryInfo:
    ok: bool
    before: int
    after: int
    rounds: int
    exhausted: bool


# Stessi marcatori del `pulisci()` dentro _JS_SCAN_CHAT_LIST piu' sotto. Qui
# servono come SECONDA rete, non come sostituto: se il JS li lascia scappare
# (bug futuro, range Unicode diverso da quello catalogato) title_is_number
# non deve comunque leggere un numero come se fosse un nome -- e' il segnale
# che impedisce a M2/M3 di salvare un numero in chiaro in chat_title (P12).
_BIDI_MARKERS = re.compile("[" + chr(0x202a) + "-" + chr(0x202e) + chr(0x2066) + "-" + chr(0x2069) + "]")


def title_is_number(title: str) -> bool:
    """True se `title` e' un numero puro (contatto non in rubrica) e non un
    nome. Pubblica (non solo per ChatRow, vedi scan_chat_list) perche' serve
    anche a chi legge il titolo dall'header della chat aperta
    (read_open_chat_title) con lo stesso identico giudizio P12: mai salvare
    un numero in chiaro come se fosse un nome."""
    return _BIDI_MARKERS.sub("", title or "").replace(" ", "").replace("+", "").isdigit()


@dataclass
class ChatRow:
    position: int
    title: str
    title_is_number: bool
    unread_count: int
    preview: str
    last_is_outbound: bool
    outgoing_state: str | None
    muted: bool
    # "Messaggi a te stesso": riga speciale inclusa apposta nello scan (serve
    # ad altri chiamanti), ma il suo titolo e' il nome del TITOLARE del
    # numero, non di un contatto -- chi consuma righe[0] per identificare
    # "la chat appena scritta" deve escluderla esplicitamente (bug trovato
    # 08/08: _impara_chat_title la prendeva per buona, 4/4 sull'invio reale).
    is_yourself: bool = False


def classify_direction(*, aria_tu: bool, tail_icon: str | None,
                       data_id: str | None) -> str:
    """'out' se ALMENO UN segnale dice OUT e NESSUNO dice IN. Altrimenti 'in'.

    Funzione pura a livello di modulo, non un metodo: e' la regola piu'
    importante del canale e deve essere testabile senza browser.

    Asimmetrica di proposito: leggere un messaggio in piu' costa un po' di
    tempo, saltarne uno costa un opt-out violato.
    """
    dice_in = False
    dice_out = False
    if aria_tu:
        dice_out = True
    if tail_icon == "tail-out":
        dice_out = True
    elif tail_icon == "tail-in":
        dice_in = True
    if data_id:
        if data_id.startswith(sel.DATA_ID_OUT_PREFIX) and len(data_id) == sel.DATA_ID_OUT_LEN:
            dice_out = True
        elif data_id.startswith(sel.DATA_ID_IN_PREFIX) and len(data_id) == sel.DATA_ID_IN_LEN:
            dice_in = True
    return "out" if (dice_out and not dice_in) else "in"


def _elapsed_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _parse_unread(raw: str | None) -> int:
    """Da testo libero ('3', '3 messaggi non letti', '') a intero. Un badge
    senza cifre ma con del testo (icona sola) vale comunque 1 non letto.
    None (DOM ostile: il JS reale _JS_SCAN_CHAT_LIST torna sempre una
    stringa, vedi il suo ramo `u ? ... : ''`, ma un chiamante ostile o un
    futuro bug potrebbe non farlo) si tratta come stringa vuota: nessuna
    eccezione, mai un TypeError su `for ch in None`."""
    raw = raw or ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else (1 if raw else 0)


# Le quattro chiavi che _JS_MESSAGE_SIGNALS promette per ogni riga. Usate per
# ESTENDERE la sentinella di cecita' oltre al caso rows is None: se domani
# qualcuno tocca il JS senza toccare read_inbound_tail (o viceversa) e una
# riga esce incompleta, un default silenzioso (.get(..., "")) produrrebbe un
# inbound svuotato di testo -- non None, ma comunque invisibile a un
# controllo di STOP. Stesso effetto pratico della cecita', quindi stessa
# risposta: None, non un valore fabbricato.
_MESSAGE_SIGNAL_KEYS = frozenset({"aria_tu", "tail_icon", "data_id", "text"})


def _riga_valida(row) -> bool:
    """Una riga e' valida solo se ha le 4 chiavi attese CON I TIPI PROMESSI
    dal JS: aria_tu e' sempre un bool (`!!...`), tail_icon/data_id sono
    stringa o None, text e' SEMPRE una stringa (slice di innerText, mai
    null). Un `text=None` che arrivasse qui sarebbe malformato quanto una
    chiave mancante: se finisse in coda cosi' com'e', un chiamante che fa
    'STOP' in t.upper() esploderebbe. Stessa famiglia di rischio, stessa
    risposta -- cecita' dell'INTERA lettura (vedi _righe_ben_formate), non
    uno scarto silenzioso della singola riga."""
    return (
        isinstance(row, dict)
        and _MESSAGE_SIGNAL_KEYS.issubset(row)
        and isinstance(row["aria_tu"], bool)
        and isinstance(row["tail_icon"], (str, type(None)))
        and isinstance(row["data_id"], (str, type(None)))
        and isinstance(row["text"], str)
    )


def _righe_ben_formate(rows: list) -> bool:
    return all(_riga_valida(row) for row in rows)


# Estrae i segnali GREZZI di direzione per ogni messaggio del pannello
# conversazione, in ordine DOM. La classificazione resta in Python
# (classify_direction): un'unica implementazione testabile senza browser,
# non due (una qui in JS e una in Python) che potrebbero divergere.
#
# Sentinella di cecita' (SDD 6.4): se il selettore combinato non aggancia
# NESSUNA bolla, torna null -- e' rotto il DOM o la chat non e' aperta, non
# "zero messaggi". Tornare [] in questo caso farebbe concludere al chiamante
# "nessuno STOP" e invierebbe SEMPRE, sembrando funzionare.
_JS_MESSAGE_SIGNALS = """
(args) => {
  const rows = Array.from(document.querySelectorAll(args.msgRow));
  if (rows.length === 0) return null;
  return rows.map((el) => {
    let tailIcon = null;
    if (el.querySelector("[data-icon='tail-out']")) tailIcon = 'tail-out';
    else if (el.querySelector("[data-icon='tail-in']")) tailIcon = 'tail-in';
    return {
      aria_tu: !!el.querySelector("span[aria-label='Tu:']"),
      tail_icon: tailIcon,
      data_id: el.getAttribute('data-id') || null,
      text: (el.innerText || '').slice(0, 300),
    };
  });
}
"""

# Come _JS_MESSAGE_SIGNALS, con in piu' pre_plain_text -- VERIFICATO dal vivo
# il 21/08 (diag_wa_timestamp.py, su chat reali): l'attributo
# data-pre-plain-text di '.copyable-text' porta '[HH:MM, DD/MM/YYYY]
# Mittente: ', presente su ogni bolla-messaggio vera, ASSENTE sulle righe di
# sistema (es. l'avviso di crittografia end-to-end) che non hanno un
# mittente. Serve a read_inbound_since, non a read_inbound_tail: quest'
# ultimo resta invariato apposta (usato dalla guardia STOP, SDD 6.4, che non
# ha nozione di tempo e deve restare quella).
_JS_MESSAGE_SIGNALS_TIMESTAMP = """
(args) => {
  const rows = Array.from(document.querySelectorAll(args.msgRow));
  if (rows.length === 0) return null;
  return rows.map((el) => {
    let tailIcon = null;
    if (el.querySelector("[data-icon='tail-out']")) tailIcon = 'tail-out';
    else if (el.querySelector("[data-icon='tail-in']")) tailIcon = 'tail-in';
    const copy = el.querySelector('.copyable-text');
    return {
      aria_tu: !!el.querySelector("span[aria-label='Tu:']"),
      tail_icon: tailIcon,
      data_id: el.getAttribute('data-id') || null,
      text: (el.innerText || '').slice(0, 300),
      pre_plain_text: copy ? (copy.getAttribute('data-pre-plain-text') || null) : null,
    };
  });
}
"""

_RE_TIMESTAMP_WA = re.compile(r"^\[(\d{2}):(\d{2}), (\d{2})/(\d{2})/(\d{4})\]")


def parse_wa_timestamp(pre_plain_text):
    """Timestamp del messaggio dal prefisso data-pre-plain-text di WhatsApp
    Web, VERIFICATO dal vivo il 21/08 (diag_wa_timestamp.py). Fuso fisso
    Europe/Rome via zoneinfo (verificato disponibile su questa macchina il
    21/08 -- niente fallback UTC+1 costante, l'incidente noto di tzdata
    mancante): il testo che WhatsApp mostra e' sempre l'ora LOCALE del
    dispositivo, che per questo numero e' sempre l'Italia.

    None se il prefisso manca (righe di sistema come l'avviso di
    crittografia, che non hanno data-pre-plain-text affatto -- vedi
    classify_direction, che le conta 'in' per sicurezza sullo STOP: qui
    invece si scartano, mai una data indovinata) o non combacia col
    formato atteso."""
    from zoneinfo import ZoneInfo

    if not pre_plain_text:
        return None
    m = _RE_TIMESTAMP_WA.match(pre_plain_text)
    if not m:
        return None
    hh, mm, dd, mo, yyyy = (int(x) for x in m.groups())
    try:
        return datetime(yyyy, mo, dd, hh, mm, tzinfo=ZoneInfo("Europe/Rome"))
    except ValueError:
        return None

# Scan della lista chat (sidebar). Nessun click su una riga: uno scan che
# apre una chat per capire qualcosa la marca anche come letta.
_JS_SCAN_CHAT_LIST = """
(args) => {
  const pane = document.querySelector(args.pane);
  if (!pane) return {error: 'pane non trovato'};

  // Marcatori di direzione del testo (U+202A-U+202E, U+2066-U+2069) che
  // WhatsApp infila dentro gli attributi title. Invisibili a schermo, ma
  // sporcano confronti e ricerche -- su console Windows cp1252 hanno gia'
  // ucciso uno script del PoC (27/07).
  const pulisci = (s) => (s || '').replace(/[\\u202a-\\u202e\\u2066-\\u2069]/g, '').trim();

  // Le INTESTAZIONI di sezione sono [role='row'] come le chat vere: una riga
  // vera ha sempre un cell-frame-title, o e' la riga speciale "chat con se
  // stessi" (che non ne ha uno).
  const rows = Array.from(pane.querySelectorAll(args.row))
    .filter(r => r.querySelector(args.rowMarker) || r.matches(args.yourself));

  return rows.map((r, i) => {
    const t = r.querySelector(args.title);
    const u = r.querySelector(args.unread);
    const p = r.querySelector(args.preview);

    // Direzione dell'ultimo messaggio in sidebar (Q42): nessuna riga ha un
    // data-icon, le spunte sono <svg><title>wds-ic-*</title></svg>.
    // Presenza -> l'ultimo messaggio e' NOSTRO (misurato su 68 righe: 5
    // read, 1 delivered, 62 senza icona = ultimo messaggio dell'altro).
    let outgoingState = null;
    for (const svg of r.querySelectorAll('svg')) {
      const ti = svg.querySelector('title');
      const nome = ti ? ti.textContent : '';
      if (nome && /^wds-ic-(read|delivered|sent|pending|check)/.test(nome)) {
        outgoingState = nome;
        break;
      }
    }

    return {
      position: i,
      title: pulisci(t ? (t.getAttribute('title') || t.innerText) : ''),
      unread_raw: u ? (u.innerText || u.getAttribute('aria-label') || '') : '',
      preview: pulisci(p ? (p.getAttribute('title') || p.innerText) : ''),
      last_is_outbound: outgoingState !== null,
      outgoing_state: outgoingState,
      muted: !!r.querySelector(args.muted),
      is_yourself: r.matches(args.yourself),
    };
  });
}
"""


class WhatsAppWebPage:
    """POM per una sessione WhatsApp Web gia' aperta su una pagina Playwright/
    Patchright. Non apre ne' chiude il browser: quello e' compito del
    chiamante (mini-sessione per-numero, M3), esattamente come InstagramPage
    non apre il proprio context."""

    def __init__(self, page):
        self._page = page

    async def _first_locator(self, candidates: list[str], timeout_ms: int = 4000):
        """Prova N selettori in ordine, torna (locator, selettore_che_ha_funzionato)
        o None. Il DOM di WhatsApp Web e' offuscato e cambia: nessun metodo
        di questo POM deve dipendere da UN solo selettore."""
        for candidate in candidates:
            try:
                loc = self._page.locator(candidate).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                return loc, candidate
            except Exception:
                continue
        return None

    async def session_state(self) -> Literal["logged_in", "qr_required", "unknown"]:
        """Timeout tarati sull'incidente del 27/07 (poc1_login.py, vedi
        SESSION_STATE_TIMEOUT_* in whatsapp_selectors.py): un profilo freddo
        puo' costruire l'app WhatsApp Web in oltre un minuto. Un timeout
        corto qui produce un falso 'qr_required' su una sessione sana -- e un
        chiamante che ci crede rifa' il QR, azzerando PoC-1."""
        if await self._first_locator(sel.CHATLIST, timeout_ms=sel.SESSION_STATE_TIMEOUT_CHATLIST_MS):
            return "logged_in"
        if await self._first_locator(sel.QR, timeout_ms=sel.SESSION_STATE_TIMEOUT_QR_MS):
            return "qr_required"
        return "unknown"

    async def _svuota_ricerca(self, box) -> bool:
        """Svuota la casella di ricerca e conferma che sia VERAMENTE vuota.

        Senza questo, il secondo numero di un ciclo si accoda al primo e la
        ricerca fallisce (misurato il 27/07). Esce anche da una chat
        eventualmente aperta al giro precedente (Escape): con una
        conversazione aperta il focus puo' finire sul composer, e li'
        Ctrl+A + Delete cancellerebbe una bozza invece della ricerca.
        """
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await box.click()
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await self._page.keyboard.press("Control+A")
        await self._page.keyboard.press("Delete")
        await asyncio.sleep(random.uniform(0.2, 0.4))
        try:
            residuo = (await box.inner_text()).strip()
        except Exception:
            residuo = ""
        if residuo:
            for _ in range(len(residuo) + 5):
                await self._page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.03, 0.09))
            try:
                residuo = (await box.inner_text()).strip()
            except Exception:
                residuo = ""
        return residuo == ""

    async def _apri_chat_da_risultati(self, timeout_ms: int = 8000) -> tuple[bool, str, bool]:
        """Apre la chat 1:1 dai risultati di ricerca, navigando PER SEZIONE.

        Ne' Enter ne' 'la prima riga': la riga 0 e' l'intestazione 'Chat', e
        sotto 'Gruppi in comune' ci sono GRUPPI, fuori perimetro -- aprirli
        per sbaglio li marca anche come letti.

        Terzo valore di ritorno: True se la riga aveva il badge 'non letto'
        PRIMA del click che la apre (e la marca letta) -- letto con lo
        stesso UNREAD_BADGE gia' verificato per la sidebar, solo scoped alla
        riga di risultato invece che alla lista chat (stessa componente
        DOM). Serve a chi apre una chat per un'operazione una tantum e deve
        poter dire dopo quali erano davvero da leggere."""
        righe = self._page.locator(sel.ROW)
        try:
            await righe.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return False, "nessun-risultato-di-ricerca", False

        n = await righe.count()
        testi = []
        for i in range(n):
            try:
                testi.append((await righe.nth(i).inner_text()).strip())
            except Exception:
                testi.append("")

        idx = next((i for i, t in enumerate(testi) if t.lower() in ("chat", "chats")), None)
        if idx is None:
            return False, "nessuna-sezione-chat:solo-gruppi-o-contatti-senza-conversazione", False
        if idx + 1 >= n or testi[idx + 1].lower() in sel.SEARCH_RESULT_HEADERS:
            return False, "sezione-chat-vuota:nessuna-conversazione-esistente", False

        riga_target = righe.nth(idx + 1)
        try:
            era_non_letto = await riga_target.locator(sel.UNREAD_BADGE).count() > 0
        except Exception:
            era_non_letto = False
        await riga_target.click()
        return True, f"aperta-riga-{idx + 1}", era_non_letto

    async def _history_signal(self) -> str:
        try:
            await self._page.locator(sel.MSG_ROW).first.wait_for(state="visible", timeout=5000)
        except Exception:
            return "nessuna-cronologia:nessun-messaggio-nel-pannello"
        count = await self._page.locator(sel.MSG_ROW).count()
        return f"cronologia:{sel.MSG_ROW}:{count}"

    async def open_chat(self, e164: str) -> OpenResult:
        """Apre una chat ESISTENTE cercando il numero. SOLO ricerca, MAI
        deep-link nemmeno come fallback (SDD 6.4 punto 1, V2): su un numero
        senza chat il deep-link ne creerebbe una nuova.

        `ok` riflette solo se il composer e' apparso; la presenza/assenza di
        cronologia sta in `signal`, per intero -- il POM non decide se la V2
        blocca l'invio, quella e' una scelta del chiamante (M3).
        """
        t0 = time.perf_counter()
        masked = mask_phone(e164)

        found = await self._first_locator(sel.SEARCH, timeout_ms=10000)
        if not found:
            logger.warning(f"open_chat({masked}): casella di ricerca non trovata")
            return OpenResult(False, _elapsed_ms(t0), "nessuna-cronologia:casella-ricerca-non-trovata")
        box, _ = found

        if not await self._svuota_ricerca(box):
            logger.warning(f"open_chat({masked}): ricerca non svuotata")
            return OpenResult(False, _elapsed_ms(t0), "nessuna-cronologia:ricerca-non-svuotata")

        # human_type, non un delay fisso: un ritardo costante su dodici cifre
        # consecutive e' varianza zero, la firma robotica piu' banale.
        await human_input.human_type(self._page, box, e164)
        await self._page.wait_for_timeout(2500)

        # Il focus deve essere ANCORA sulla ricerca. Se e' finito altrove
        # (es. su un composer gia' aperto da un giro precedente), un tasto
        # qualsiasi finirebbe dentro un messaggio -- inaccettabile per un
        # metodo che in M1 non deve mai inviare nulla.
        focused = await box.evaluate("el => el === document.activeElement")
        if not focused:
            logger.warning(f"open_chat({masked}): focus perso prima della selezione")
            return OpenResult(False, _elapsed_ms(t0), "nessuna-cronologia:focus-non-sulla-ricerca-pre-invio")

        aperto, nota, era_non_letto = await self._apri_chat_da_risultati()
        if not aperto:
            return OpenResult(False, _elapsed_ms(t0), f"nessuna-cronologia:{nota}")

        composer = await self._first_locator(sel.COMPOSER, timeout_ms=15000)
        signal = await self._history_signal()
        ms = _elapsed_ms(t0)
        ok = bool(composer)
        logger.info(f"open_chat({masked}): ok={ok} ms={round(ms)} signal={signal}")
        return OpenResult(ok, ms, signal, era_non_letto=era_non_letto)

    async def read_open_chat_title(self) -> str | None:
        """Titolo (nome o numero) della chat GIA' APERTA, letto direttamente
        dal suo header -- MAI dalla sidebar (fix 21/08: leggere la sidebar
        dopo l'invio per dedurre 'la prima riga e' il contatto appena
        scritto' e' un'assunzione di posizione, falsa ogni volta che
        un'altra chat molto attiva o pinnata scavalca quella giusta --
        misurato dal vivo: 283 contatti di una sola campagna con lo stesso
        chat_title sbagliato, quello della chat che stava scavalcando).

        None se l'header non si trova o e' vuoto: chi chiama rinuncia a
        imparare il titolo per questa volta, non inventa nulla.

        L'header porta anche sottotitoli (stato online, 'sta scrivendo...',
        n. partecipanti per i gruppi): si prende solo la prima riga di
        inner_text, che nella struttura osservata (poc4_info_panel.py,
        09-10/08, 20/20 chat reali) e' sempre il nome/numero."""
        found = await self._first_locator(sel.HEADER, timeout_ms=4000)
        if not found:
            return None
        header, _ = found
        try:
            testo = await header.inner_text()
        except Exception:
            return None
        prima_riga = (testo or "").split("\n")[0].strip()
        return prima_riga or None

    async def load_history(self, minimo: int = 80) -> HistoryInfo:
        """Scrolla la conversazione verso l'alto finche' non ha caricato
        abbastanza messaggi (o finche' non ne arrivano piu').

        La conversazione e' VIRTUALIZZATA: nel DOM ci sono solo i messaggi
        della finestra visibile. Su una chat attiva ne restavano 17, tutti
        degli ultimi 3 minuti -- uno STOP di venti minuti prima non esisteva
        proprio nel DOM. Il caricamento e' PARTE della guardia, non un
        accessorio (SDD 6.4).

        Si scrolla con la ROTELLINA e non con scrollTop: genera eventi di
        scroll veri, che una sessione automatizzata altrimenti non produce
        mai (SDD 6.4).

        NON chiama read_inbound_tail da sola: separate, servono a M3 per
        rileggere la coda a costo basso subito prima di un invio (finestra
        TOCTOU di ~20s misurata in M0).
        """
        n_prima = await self._page.locator(sel.MSG_ROW).count()
        box = await self._page.locator(sel.MAIN).bounding_box()
        if not box:
            return HistoryInfo(ok=False, before=n_prima, after=n_prima, rounds=0, exhausted=False)

        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await self._page.mouse.move(cx, cy)

        giri, fermi, max_giri = 0, 0, 20
        n = n_prima
        while n < minimo and giri < max_giri and fermi < 3:
            await self._page.mouse.wheel(0, -random.randint(700, 1400))
            await asyncio.sleep(random.uniform(0.5, 1.1))
            nuovo = await self._page.locator(sel.MSG_ROW).count()
            # "fermi" conta i giri che non hanno portato nulla: la
            # conversazione puo' essere semplicemente finita (inizio chat).
            fermi = fermi + 1 if nuovo == n else 0
            n = nuovo
            giri += 1

        return HistoryInfo(ok=True, before=n_prima, after=n, rounds=giri, exhausted=fermi >= 3)

    async def read_inbound_tail(self, n: int = 40) -> list[str] | None:
        """Ultimi `n` messaggi INBOUND, ovunque siano nella coda -- non si
        ferma al primo nostro (SDD 6.4): altrimenti una risposta manuale
        dopo uno STOP lo renderebbe invisibile per sempre.

        None = cecita' (nessuna bolla agganciata nel DOM), diverso da [] =
        silenzio (bolle presenti, nessun inbound). Se questo tornasse []
        anche in caso di cecita', il chiamante concluderebbe 'nessuno STOP'
        e invierebbe SEMPRE, sembrando funzionare.

        La sentinella copre anche il caso "il JS ha risposto ma con righe
        malformate" (chiave mancante, riga non-dict): un default silenzioso
        in quel punto produrrebbe un inbound svuotato di testo, invisibile a
        un controllo di STOP quanto una lista vuota fabbricata -- stesso
        rischio di `rows is None`, stessa risposta.

        NON carica da sola la cronologia (SDD 6.4 punto 2): quella e'
        load_history, il chiamante decide quando invocarle in sequenza.
        Separate cosi' M3 puo' rileggere la coda a costo basso (cronologia
        gia' caricata) subito prima di premere invio.
        """
        rows = await self._page.evaluate(_JS_MESSAGE_SIGNALS, {"msgRow": sel.MSG_ROW})
        # `not rows` copre sia None (cecita' dichiarata dal JS) sia [] (che il
        # JS di _JS_MESSAGE_SIGNALS non produce MAI legittimamente: promette
        # null per zero righe agganciate, vedi il suo commento sopra). Una
        # lista vuota qui e' quindi una violazione di contratto -- stessa
        # cecita', non un silenzio: il silenzio vero (bolle presenti, tutte
        # OUT) si produce DOPO il filtro di classify_direction, non prima.
        if not rows or not _righe_ben_formate(rows):
            return None

        tail: list[str] = []
        for row in reversed(rows):
            direzione = classify_direction(
                aria_tu=row["aria_tu"],
                tail_icon=row["tail_icon"],
                data_id=row["data_id"],
            )
            if direzione == "out":
                continue
            tail.append(row["text"])
            if len(tail) >= n:
                break
        tail.reverse()
        return tail

    async def read_inbound_since(self, dopo: datetime, *, entro: datetime | None = None,
                                 n: int = 100) -> list[str] | None:
        """Testi INBOUND con timestamp reale (data-pre-plain-text) rigorosamente
        DOPO `dopo`, e se `entro` e' dato non oltre `entro`. A differenza di
        read_inbound_tail (SDD 6.4, guardia pre-invio STOP) questo metodo ha
        nozione di TEMPO -- serve a un compito diverso: distinguere una
        risposta VERA a un nostro invio da corrispondenza organica
        precedente sullo stesso numero (il canale e' condiviso con
        l'assistenza clienti umana, non nasce con la campagna) o da righe
        di sistema senza mittente riconoscibile.

        L'avviso 'i messaggi sono crittografati end-to-end' non ha
        data-pre-plain-text (nessun mittente): classify_direction lo conta
        'in' per sicurezza sullo STOP (corretto li', quella funzione non
        deve MAI perdere un vero STOP), ma userebbe qui a produrre un falso
        'ha risposto' -- misurato dal vivo il 21/08 nel backfill del bug
        chat_title: 3 falsi positivi su 4 nel pilota, proprio per questo.
        Qui si scarta ogni riga senza timestamp leggibile, non si indovina.

        None = cecita' (nessuna bolla agganciata), stessa sentinella di
        read_inbound_tail. [] = nessuna risposta genuina nella finestra --
        silenzio vero e rumore-senza-timestamp sono indistinguibili qui di
        proposito, per questo scopo hanno lo stesso esito."""
        rows = await self._page.evaluate(_JS_MESSAGE_SIGNALS_TIMESTAMP, {"msgRow": sel.MSG_ROW})
        if not rows or not _righe_ben_formate(rows):
            return None

        testi: list[str] = []
        for row in rows:
            direzione = classify_direction(
                aria_tu=row["aria_tu"], tail_icon=row["tail_icon"], data_id=row["data_id"])
            if direzione == "out":
                continue
            ts = parse_wa_timestamp(row.get("pre_plain_text"))
            if ts is None or ts <= dopo:
                continue
            if entro is not None and ts > entro:
                continue
            testi.append(row["text"])
            if len(testi) >= n:
                break
        return testi

    async def sync_state(self) -> Literal["synced", "syncing", "unknown"]:
        """A9/FM16: su una chat non ancora sincronizzata la guardia non
        legge un silenzio, legge il VUOTO. Il selettore dell'indicatore non
        e' catalogato -- catturarlo richiede un re-scan del QR, che azzera
        PoC-1 -- quindi torna SEMPRE 'unknown' finche' SYNC_INDICATOR resta
        vuoto. NON torna 'synced': e' la politica (M3) a decidere cosa fare
        di 'unknown', non questo metodo.
        """
        if not sel.SYNC_INDICATOR:
            return "unknown"
        found = await self._first_locator(sel.SYNC_INDICATOR, timeout_ms=3000)
        return "syncing" if found else "synced"

    async def send_text(self, text: str) -> None:
        """Digita e invia nel composer della chat GIA' APERTA. Non apre la
        chat, non controlla opt-out/STOP: quelle guardie stanno nel
        chiamante (wa_sender, M3).

        In M1 esiste per completare l'interfaccia del POM ma NON va
        esercitato contro WhatsApp vero: gli invii sono M3, dopo cap e
        guardie.
        """
        found = await self._first_locator(sel.COMPOSER, timeout_ms=10000)
        if not found:
            raise RuntimeError("Composer non trovato: impossibile inviare")
        composer, _ = found
        await human_input.human_type(self._page, composer, text)
        await asyncio.sleep(random.uniform(0.4, 1.2))
        await self._page.keyboard.press("Enter")

    async def read_last_tick(self) -> str:
        """Spunta dell'ultimo messaggio (aria-label, non data-icon: Q39).
        ATTENZIONE: testo LOCALIZZATO IN ITALIANO (SDD A4)."""
        found = await self._first_locator(sel.TICKS, timeout_ms=sel.READ_LAST_TICK_TIMEOUT_MS)
        return found[1] if found else "nessuna-spunta-letta"

    async def scan_chat_list(self) -> list[ChatRow]:
        """Scan della sidebar, NESSUN click su una riga chat (una chat
        aperta per sbaglio viene marcata come letta). Titoli e preview
        arrivano GREZZI: il POM non decide opt-out (has_stop e' un giudizio
        di M3, non un segnale del DOM)."""
        args = {
            "pane": sel.CHATLIST[0], "row": sel.ROW, "rowMarker": sel.ROW_MARKER,
            "yourself": sel.YOURSELF_ROW, "title": sel.TITLE, "unread": sel.UNREAD_BADGE,
            "preview": sel.PREVIEW, "muted": sel.MUTED,
        }
        result = await self._page.evaluate(_JS_SCAN_CHAT_LIST, args)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"scan_chat_list: {result['error']} -- ricontrolla i selettori del catalogo.")

        rows: list[ChatRow] = []
        for r in result:
            try:
                title = r["title"]
                rows.append(ChatRow(
                    position=r["position"],
                    title=title,
                    title_is_number=title_is_number(title),
                    unread_count=_parse_unread(r["unread_raw"]),
                    preview=r["preview"],
                    last_is_outbound=r["last_is_outbound"],
                    outgoing_state=r["outgoing_state"],
                    muted=r["muted"],
                    is_yourself=r["is_yourself"],
                ))
            except KeyError as exc:
                # Un KeyError grezzo qui dentro il ciclo non dice a chi debugga
                # DOVE guardare: nomina i selettori del catalogo (TITLE/
                # UNREAD_BADGE/PREVIEW/MUTED in whatsapp_selectors.py) invece
                # di un'eccezione muta.
                raise RuntimeError(
                    f"scan_chat_list: riga senza la chiave {exc} -- probabile "
                    "disallineamento tra _JS_SCAN_CHAT_LIST e i selettori "
                    "TITLE/UNREAD_BADGE/PREVIEW/MUTED del catalogo in "
                    "whatsapp_selectors.py (ricontrollarli)."
                ) from exc
        return rows
