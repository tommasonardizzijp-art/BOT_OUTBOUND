"""PoC-4 (09-10/08, esteso il 10/08 con RAM libera): misura il pannello
info-contatto, la barra liste/etichette e il riconoscimento gruppo su
WhatsApp Web -- sul numero VERO di Primero, unica finestra utile perche'
serve una sessione viva con le sue chat reali (handoff
2026-08-08-whatsapp-onboarding-primero-AVVIO.md, punto 6). SOLA LETTURA:
nessun invio, nessun testo digitato.

Campione 20 chat (era 8 nel primo giro con RAM quasi esaurita -- 75MB
liberi su 7.7GB con due campagne Instagram attive; ora 423MB liberi dopo
uno shutdown pulito, si puo' allargare). Seconda meta' del campione presa
DOPO uno scroll della lista, apposta per non misurare solo le chat piu'
recenti/gia' aperte nei test precedenti e per avere una misura vera del
costo di scroll (punto 4 dell'handoff: secondi per chat).

Nessun selettore per pannello info/barra liste esisteva nel catalogo prima
di questo giro (wa-dom-catalog.md non li menzionava): qui si esplora con
candidati multipli, si registra quale aggancia, si documenta il resto come
buco misurato -- non si inventano selettori. Screenshot automatico su ogni
fallimento (pannello/header non trovato): senza, un selettore sbagliato
lascia solo un "errore" testuale e il giro va rifatto per capire perche'.
"""
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

from app.database import AsyncSessionLocal
from app.browser.whatsapp_page import WhatsAppWebPage
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise

NUMERO = "8c578c08-6659-43fa-9840-f55e88e220fc"
CAMPIONE_VISIBILE = 12
CAMPIONE_DOPO_SCROLL = 8
OUT_DIR = Path(__file__).parent / "artifacts"

# Candidati per il click che apre il pannello destro (mai verificati prima).
HEADER_CANDIDATI = [
    "header[data-testid='conversation-header']",
    "#main header",
]

# Candidati per il pannello risultante.
PANNELLO_CANDIDATI = [
    "[data-testid='drawer-right']",
    "[data-testid='contact-info-drawer']",
    "div[role='complementary']",
    "aside",
]

# Barra liste/etichette: candidati sopra la lista chat (WhatsApp Business).
LISTE_CANDIDATI = [
    "div[aria-label*='Liste']",
    "div[aria-label*='Etichette']",
    "div[aria-label*='Filtro']",
    "[data-testid='chat-list-filters']",
    "div[role='tablist']",
]

RE_TELEFONO = re.compile(r"(\+?\d[\d\s\-\(\)]{7,}\d)")
RE_PARTECIPANTI = re.compile(r"(\d+)\s*(partecipant|membr|iscritt)", re.IGNORECASE)


async def _trova_primo(page, candidati: list[str], timeout_ms: int = 2500):
    for sel in candidati:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return sel, loc
        except Exception:
            continue
    return None, None


async def misura_liste_etichette(page) -> dict:
    sel, loc = await _trova_primo(page, LISTE_CANDIDATI, timeout_ms=3000)
    if not loc:
        return {"trovato": False, "selettore": None, "testo": None}
    testo = await loc.inner_text()
    return {"trovato": True, "selettore": sel, "testo": testo[:300]}


async def _screenshot_debug(page, tag: str) -> str | None:
    try:
        path = OUT_DIR / f"debug_{tag}_{int(time.time())}.png"
        await page.screenshot(path=str(path))
        return str(path)
    except Exception:
        return None


async def misura_chat(page, indice: int, riga_titolo: str, riga_title_is_number: bool) -> dict:
    t0 = time.perf_counter()
    riga = page.locator("[role='row']").nth(indice)
    esito = {"indice": indice, "titolo_sidebar": riga_titolo,
             "titolo_e_numero": riga_title_is_number, "aperta": False,
             "pannello_trovato": False, "pannello_selettore": None,
             "numero_leggibile": False, "numero_grezzo": None,
             "sembra_gruppo": False, "screenshot_debug": None, "errore": None}
    try:
        await riga.click(timeout=5000)
        esito["aperta"] = True
        await page.wait_for_timeout(random.randint(600, 1200))

        header_sel, header_loc = await _trova_primo(page, HEADER_CANDIDATI, timeout_ms=4000)
        if not header_loc:
            esito["errore"] = "header non trovato"
            esito["screenshot_debug"] = await _screenshot_debug(page, f"no_header_{indice}")
            return esito

        header_testo = await header_loc.inner_text()
        if RE_PARTECIPANTI.search(header_testo):
            esito["sembra_gruppo"] = True

        await header_loc.click(timeout=4000)
        await page.wait_for_timeout(random.randint(500, 900))

        pannello_sel, pannello_loc = await _trova_primo(page, PANNELLO_CANDIDATI, timeout_ms=3500)
        if pannello_loc:
            esito["pannello_trovato"] = True
            esito["pannello_selettore"] = pannello_sel
            testo_pannello = await pannello_loc.inner_text()
            if RE_PARTECIPANTI.search(testo_pannello):
                esito["sembra_gruppo"] = True
            m = RE_TELEFONO.search(testo_pannello)
            if m:
                esito["numero_leggibile"] = True
                esito["numero_grezzo"] = m.group(1)
            else:
                # Pannello trovato ma nessun numero dentro: e' un fallimento
                # DIVERSO da "pannello non trovato" (selettore giusto, dato
                # assente/diverso) -- vale la pena vedere cosa c'e' davvero.
                esito["screenshot_debug"] = await _screenshot_debug(page, f"no_numero_{indice}")
        else:
            esito["errore"] = "pannello non trovato dopo click header"
            esito["screenshot_debug"] = await _screenshot_debug(page, f"no_pannello_{indice}")

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        await page.keyboard.press("Escape")
    except Exception as exc:
        esito["errore"] = f"{type(exc).__name__}: {exc}"
        esito["screenshot_debug"] = await _screenshot_debug(page, f"eccezione_{indice}")
    finally:
        esito["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return esito


async def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, NUMERO)
        proxy_url = numero.proxy_url

    risultati: dict = {"liste_etichette": None, "chat": [], "scroll_ms": None,
                       "righe_dom_prima_scroll": None, "righe_dom_dopo_scroll": None}

    async with _open_wa_browser(NUMERO, headless=True, proxy_url=proxy_url) as context:
        page = await context.new_page()
        await page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded")
        pom = WhatsAppWebPage(page)
        stato = await pom.session_state()
        print(f"session_state={stato}", file=sys.stderr)
        if stato != "logged_in":
            print(json.dumps({"errore": f"sessione non loggata: {stato}"}))
            return

        righe = await pom.scan_chat_list()
        risultati["righe_dom_prima_scroll"] = len(righe)
        print(f"righe in DOM (prima dello scroll): {len(righe)}", file=sys.stderr)

        risultati["liste_etichette"] = await misura_liste_etichette(page)

        n1 = min(CAMPIONE_VISIBILE, len(righe))
        for i in range(n1):
            esito = await misura_chat(page, i, righe[i].title, righe[i].title_is_number)
            risultati["chat"].append(esito)
            print(f"[{i+1}/{n1}] leggibile={esito['numero_leggibile']} "
                 f"gruppo={esito['sembra_gruppo']} ms={esito['ms']}", file=sys.stderr)
            await page.wait_for_timeout(random.randint(800, 1800))

        # Scroll per misurare sia il costo (punto 4) sia un campione diverso
        # dalle chat piu' recenti (evita di misurare solo quelle gia' toccate
        # nei test/campagne dei giorni scorsi).
        t_scroll0 = time.perf_counter()
        pane = page.locator("#pane-side").first
        for _ in range(6):
            await pane.evaluate("el => el.scrollBy(0, 800)")
            await page.wait_for_timeout(random.randint(300, 600))
        risultati["scroll_ms"] = round((time.perf_counter() - t_scroll0) * 1000, 1)

        righe_dopo = await pom.scan_chat_list()
        risultati["righe_dom_dopo_scroll"] = len(righe_dopo)
        print(f"righe in DOM (dopo scroll, {risultati['scroll_ms']}ms): "
             f"{len(righe_dopo)}", file=sys.stderr)

        # Dopo lo scroll le posizioni [role='row'] ripartono da 0 nel DOM
        # virtualizzato (le vecchie righe sono state smontate): si campiona
        # dalla CODA della lista appena scansionata, cosi' non si ripescano
        # per errore le stesse gia' misurate sopra.
        offset = max(0, len(righe_dopo) - CAMPIONE_DOPO_SCROLL)
        n2 = min(CAMPIONE_DOPO_SCROLL, len(righe_dopo) - offset)
        for j in range(n2):
            i = offset + j
            esito = await misura_chat(page, i, righe_dopo[i].title, righe_dopo[i].title_is_number)
            esito["dopo_scroll"] = True
            risultati["chat"].append(esito)
            print(f"[scroll {j+1}/{n2}] leggibile={esito['numero_leggibile']} "
                 f"gruppo={esito['sembra_gruppo']} ms={esito['ms']}", file=sys.stderr)
            await page.wait_for_timeout(random.randint(800, 1800))

    out_path = OUT_DIR / "poc4_info_panel.json"
    out_path.write_text(json.dumps(risultati, ensure_ascii=False, indent=2), encoding="utf-8")

    tot = len(risultati["chat"])
    leggibili = sum(1 for c in risultati["chat"] if c["numero_leggibile"])
    pannello_ok = sum(1 for c in risultati["chat"] if c["pannello_trovato"])
    gruppi = sum(1 for c in risultati["chat"] if c["sembra_gruppo"])
    tempi = [c["ms"] for c in risultati["chat"] if "ms" in c]
    riepilogo = {
        "campione_totale": tot,
        "pannello_trovato": f"{pannello_ok}/{tot}",
        "numero_leggibile": f"{leggibili}/{tot}",
        "sembra_gruppo": f"{gruppi}/{tot}",
        "liste_etichette_trovate": risultati["liste_etichette"]["trovato"],
        "ms_medio_per_chat": round(sum(tempi) / len(tempi), 1) if tempi else None,
        "scroll_ms_per_6_pagine": risultati["scroll_ms"],
        "righe_dom_prima_vs_dopo_scroll":
            f"{risultati['righe_dom_prima_scroll']} -> {risultati['righe_dom_dopo_scroll']}",
        "output": str(out_path),
    }
    print(json.dumps(riepilogo, ensure_ascii=False, indent=2))


asyncio.run(main())
