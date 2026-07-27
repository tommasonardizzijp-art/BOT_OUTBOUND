# backend/scripts/poc_wa/_common.py
"""Infrastruttura condivisa degli script PoC WhatsApp (M0).

Scelte deliberate, diverse da app/browser/context_manager.py:
- profilo su path ASSOLUTO fuori dal repo: la sessione WhatsApp deve
  sopravvivere alla cancellazione del worktree;
- nessuna dipendenza dal DB (niente account_id, niente proxy da tabella);
- NESSUNA iniezione di fingerprint: su WhatsApp Web un profilo Chromium
  vergine e persistente e' il caso normale, e in M0 vogliamo misurare la
  piattaforma, non il nostro layer anti-detect. La reintroduzione del
  fingerprint e' una decisione di M1 (rischio: puo' alterare la sessione).
"""
import asyncio
import json
import math
import os
import random
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psutil

from wa_lib import mask_pii  # type: ignore  # eseguito come script dalla sua cartella

# La console Windows di Tommaso e' cp1252 e SOLLEVA su qualunque carattere che
# non sappia codificare: uno script muore con UnicodeEncodeError solo per aver
# stampato il nome di una chat. Non e' teorico — successo il 27/07 su un
# marcatore di direzione (‪) che WhatsApp mette nelle anteprime, e i nomi
# delle chat sono pieni di emoji. Senza questo, poc3_scan e poc2_send sarebbero
# morti in mezzo al PoC per un problema di sola visualizzazione.
# errors='replace': un carattere illeggibile diventa '?', lo script continua.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass   # stream rediretto o non riconfigurabile: non e' un motivo per morire

WA_URL = "https://web.whatsapp.com/"

# Radice degli artefatti PoC, FUORI da qualunque repo git (D:\dev non e' un
# repository: verificato). Contiene numeri reali in chiaro (poc.env) e la
# sessione WhatsApp: non deve poter finire in un commit per sbaglio, e deve
# sopravvivere alla cancellazione del worktree.
POC_ROOT = Path(os.environ.get("POC_WA_ROOT", r"D:\dev\wa-poc"))
ENV_FILE = Path(os.environ.get("POC_WA_ENV_FILE", POC_ROOT / "poc.env"))


def _load_env_file(path: Path) -> None:
    """Carica KEY=VALUE da `poc.env` in os.environ, senza dipendenze esterne.

    Esiste perche' l'allowlist e' l'unica guardia tra questo PoC e un messaggio
    mandato alla persona sbagliata: passarla a mano con `set` a ogni terminale
    e' il modo piu' facile per ritrovarsi con una allowlist vuota (fail-closed,
    quindi si blocca) o peggio con quella di ieri.

    Le variabili gia' presenti nell'ambiente VINCONO sul file: un override
    puntuale da riga di comando resta possibile e non viene silenziosamente
    sovrascritto.
    """
    if not path.is_file():
        return
    for riga in path.read_text(encoding="utf-8-sig").splitlines():
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        chiave, _, valore = riga.partition("=")
        chiave = chiave.strip()
        # Niente commenti a fine riga: un numero non contiene '#', ma un
        # commento tagliato male mangerebbe l'ultimo numero della lista.
        valore = valore.strip().strip('"').strip("'")
        if chiave and chiave not in os.environ:
            os.environ[chiave] = valore


_load_env_file(ENV_FILE)

PROFILE_DIR = Path(os.environ.get("POC_WA_PROFILE_DIR", POC_ROOT / "profile"))
ARTIFACTS_DIR = Path(os.environ.get("POC_WA_ARTIFACTS", POC_ROOT / "artifacts"))
MESSAGES_FILE = Path(os.environ.get("POC_WA_MESSAGES", POC_ROOT / "messages.txt"))
PROXY_URL = os.environ.get("POC_WA_PROXY") or None


def artifacts_dir() -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scrub(value):
    """Maschera ricorsivamente: str -> mask_pii, dict/list -> elemento per elemento.

    Un valore annidato non mascherato vanificherebbe l'intero mascheramento:
    gli artefatti restano su disco per settimane e riguardano chat di clienti veri.
    """
    if isinstance(value, str):
        return mask_pii(value, keep=120)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def log_event(kind: str, **fields) -> None:
    """Append di una riga JSONL in artifacts/events.jsonl + echo a schermo.

    I campi di testo (anche annidati in dict/list) passano da mask_pii: questi
    artefatti riguardano chat di clienti veri e restano su disco per settimane.
    """
    safe = {k: _scrub(v) for k, v in fields.items()}
    rec = {"ts": _now(), "kind": kind, **safe}
    line = json.dumps(rec, ensure_ascii=False)
    with (artifacts_dir() / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _parse_proxy(url: str) -> dict | None:
    p = urlparse(url.strip())
    if not p.hostname or not p.port:
        raise ValueError(f"POC_WA_PROXY malformato: {url!r}")
    out = {"server": f"{p.scheme or 'http'}://{p.hostname}:{p.port}"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


def _profile_arg(cmdline: list[str] | None) -> str | None:
    """Estrae il valore di --user-data-dir=<path> da una cmdline, se presente."""
    for arg in cmdline or []:
        if arg.startswith("--user-data-dir="):
            return arg.split("=", 1)[1].strip('"')
    return None


def cmdline_matches_profile(cmdline: list[str] | None, profile_dir: Path = PROFILE_DIR) -> bool:
    """True se la cmdline appartiene a un Chromium lanciato su ESATTAMENTE profile_dir.

    Match sull'argomento vero (--user-data-dir=<path>), normalizzato, non su una
    substring libera della cmdline intera: un domani un profilo 'profile-2'
    accanto a 'profile' darebbe falso positivo con un containment check
    (needle in cmd), e qui il falso positivo blocca l'avvio di una sessione
    legittima (_profile_in_use e' una guardia, non un dettaglio).
    """
    value = _profile_arg(cmdline)
    if not value:
        return False
    target = os.path.normcase(os.path.normpath(str(profile_dir)))
    return os.path.normcase(os.path.normpath(value)) == target


def _profile_in_use() -> bool:
    """C'e' gia' un Chromium vivo su questo profilo?

    I lock si cancellano solo se NON c'e': cancellarli sotto una sessione viva
    (heartbeat lanciato mentre poc3_scan --loop gira) corrompe entrambe.
    """
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if cmdline_matches_profile(proc.info.get("cmdline")):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


@asynccontextmanager
async def wa_context(headless: bool = False):
    """Apre il profilo persistente su web.whatsapp.com e restituisce (context, page).

    headless=False di default: in M0 vogliamo VEDERE cosa succede, e il QR va
    inquadrato col telefono.
    """
    from patchright.async_api import async_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if _profile_in_use():
        raise SystemExit(
            f"Un altro processo sta gia' usando il profilo {PROFILE_DIR}. "
            f"Chiudilo prima di lanciare questo script: due sessioni sullo stesso "
            f"profilo si corrompono a vicenda."
        )

    # Lock lasciati da una sessione uccisa male: senza rimuoverli Chromium
    # inoltra il lancio a un PID fantasma ed esce subito (lezione IG).
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = PROFILE_DIR / lock
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    # --window-size + no_viewport invece di viewport={...}: con una finestra
    # visibile, `viewport` fa aprire Chromium alla dimensione di default e POI
    # la ridimensiona. Tommaso lo vedeva come un glitch (27/07), e la pagina lo
    # vede come un evento `resize` identico al pixel a ogni singolo avvio, per
    # 14 giorni. Un resize all'apertura e' innocuo; una firma perfettamente
    # ripetuta e' gratis da evitare. Cosi' la finestra nasce gia' giusta e
    # l'evento non si genera affatto.
    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1440,900",
    ]
    kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        no_viewport=True,
        locale="it-IT",
        timezone_id="Europe/Rome",
        args=args,
        ignore_default_args=["--enable-automation"],
    )
    if PROXY_URL:
        kwargs["proxy"] = _parse_proxy(PROXY_URL)
    else:
        args.append("--no-proxy-server")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(**kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
            yield context, page
        finally:
            await context.close()


async def snap(page, name: str) -> Path:
    """Screenshot diagnostico. ATTENZIONE: contiene PII (schermate di chat vere).
    Restano in artifacts/, mai nel repo (Q48 li tratta come materiale sensibile)."""
    path = artifacts_dir() / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{name}.png"
    await page.screenshot(path=str(path), full_page=False)
    return path


async def first_locator(page, candidates: list[str], timeout_ms: int = 4000):
    """Prova N selettori in ordine, restituisce (locator, selettore_che_ha_funzionato).

    Il DOM di WhatsApp Web e' offuscato e cambia: nessuno script di M0 deve
    dipendere da UN selettore. Quale ha funzionato finisce nel catalogo.
    """
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc, sel
        except Exception:
            continue
    return None


def _typo_char(char: str) -> str | None:
    """Vicino di tastiera QWERTY, per il typo simulato."""
    neighbors = {
        "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg",
        "g": "fh", "h": "gj", "i": "uo", "l": "kp", "m": "n", "n": "bm",
        "o": "ip", "p": "ol", "q": "wa", "r": "et", "s": "ad", "t": "ry",
        "u": "yi", "v": "cb", "w": "qe", "y": "tu", "z": "x",
    }
    opts = neighbors.get(char.lower())
    return random.choice(opts) if opts else None


async def human_type(page, element, text: str) -> None:
    """Digitazione umanizzata: copia adattata di InstagramPage._human_type
    (backend/app/browser/instagram_page.py:633).

    Copiata e non importata di proposito: InstagramPage e' accoppiato al flusso IG.
    L'estrazione del modulo condiviso `human_input` e' un task di M1 (SDD sez. 6):
    questa copia e' il banco di prova di cosa deve contenere.

    Differenza rispetto a IG: su WhatsApp Web Enter invia il messaggio, quindi gli
    a-capo si battono con Shift+Enter — stesso comportamento, va confermato in PoC-2.
    """
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    base_ms = random.uniform(40, 95)

    for line_idx, line in enumerate(text.split("\n")):
        if line_idx > 0:
            await page.keyboard.press("Shift+Enter")
            await asyncio.sleep(random.uniform(0.15, 0.5))
        words = line.split(" ")
        for i, word in enumerate(words):
            if i > 0 and random.random() < 0.07:
                await asyncio.sleep(random.uniform(0.25, 1.0))
            for char_idx, char in enumerate(word):
                if len(word) > 3 and 0 < char_idx < len(word) - 1 and random.random() < 0.08:
                    wrong = _typo_char(char)
                    if wrong:
                        await page.keyboard.type(wrong)
                        await asyncio.sleep(max(30, min(480, random.lognormvariate(math.log(base_ms), 0.45))) / 1000)
                        await asyncio.sleep(random.uniform(0.12, 0.40))
                        await page.keyboard.press("Backspace")
                        await asyncio.sleep(random.uniform(0.06, 0.20))
                delay_ms = max(30, min(480, random.lognormvariate(math.log(base_ms), 0.45)))
                await page.keyboard.type(char)
                await asyncio.sleep(delay_ms / 1000)
                if random.random() < 0.015:
                    await asyncio.sleep(random.uniform(0.2, 0.7))
            if i < len(words) - 1:
                await page.keyboard.type(" ")
                await asyncio.sleep(random.uniform(25, 80) / 1000)
