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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from wa_lib import mask_pii  # type: ignore  # eseguito come script dalla sua cartella

WA_URL = "https://web.whatsapp.com/"
PROFILE_DIR = Path(os.environ.get("POC_WA_PROFILE_DIR", r"D:\wa-poc\profile"))
ARTIFACTS_DIR = Path(os.environ.get("POC_WA_ARTIFACTS", r"D:\wa-poc\artifacts"))
PROXY_URL = os.environ.get("POC_WA_PROXY") or None


def artifacts_dir() -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(kind: str, **fields) -> None:
    """Append di una riga JSONL in artifacts/events.jsonl + echo a schermo.

    I campi di testo passano da mask_pii: questi artefatti riguardano chat di
    clienti veri e restano su disco per settimane.
    """
    safe = {k: (mask_pii(v, keep=120) if isinstance(v, str) else v) for k, v in fields.items()}
    rec = {"ts": _now(), "kind": kind, **safe}
    line = json.dumps(rec, ensure_ascii=False)
    (artifacts_dir() / "events.jsonl").open("a", encoding="utf-8").write(line + "\n")
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


@asynccontextmanager
async def wa_context(headless: bool = False):
    """Apre il profilo persistente su web.whatsapp.com e restituisce (context, page).

    headless=False di default: in M0 vogliamo VEDERE cosa succede, e il QR va
    inquadrato col telefono.
    """
    from patchright.async_api import async_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # Lock lasciati da una sessione uccisa male: senza rimuoverli Chromium
    # inoltra il lancio a un PID fantasma ed esce subito (lezione IG).
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = PROFILE_DIR / lock
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1440, "height": 900},
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
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
        try:
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
