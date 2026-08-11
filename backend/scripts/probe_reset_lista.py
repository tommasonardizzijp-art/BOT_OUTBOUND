# backend/scripts/probe_reset_lista.py
"""Task 13: sonda diagnostica sul reset della lista inbox.

Diagnostica PURA: non apre nessuna chat, non salva nulla, non tocca lo stato
della campagna. Scorre in continuazione campionando l'altezza del
contenitore ogni ~2s; quando crolla di oltre CROLLO_SOGLIA_PX in un colpo
solo (la lista si "rimonta", non "scorre su" — osservato l'11/08: due reset,
entrambi oltre i 15.000px, entrambi atterrati a ~400px), salva le ultime
richieste di rete verso gli endpoint inbox, lo stato del documento
(visibilityState, eventuali pagehide/freeze) e il numero di righe nel DOM.

Se i reset coincidono con una risposta su direct_v2/inbox, la causa e' il
refetch periodico dell'inbox (mitigazione: ripristinare scrollTop invece di
ricominciare). Se non c'e' nessuna richiesta correlata, l'ipotesi passa a un
limite interno della virtualizzazione (mitigazione praticabile: il
segnalibro, che rende il reset un inciampo da pochi secondi invece che una
ripartenza da zero).

Uso (dal folder backend, con la campagna in pausa e il profilo libero):
    ./venv/Scripts/python.exe scripts/probe_reset_lista.py [minuti]
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.services.inbox_browser.pagina import (
    _JS_CANDIDATI, _JS_RIGHE, _JS_STATO_CONTENITORE, bordo_colonne, scegli_contenitore, scorri,
)

ACCOUNT = "@michele.carozza"
_INBOX_ENDPOINT = re.compile(r"(direct_v2|graphql|api/v1)", re.I)
CROLLO_SOGLIA_PX = 2000
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "probe_reset_lista.json")


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {p(msg)}", flush=True)


async def stato_lista(page):
    """scrollTop/altezza del contenitore della lista — None se non si trova."""
    dati = await page.evaluate(_JS_RIGHE, 30)
    bordo = bordo_colonne((dati or {}).get("righe") or [])
    candidati = await page.evaluate(_JS_CANDIDATI)
    idx = scegli_contenitore(candidati, bordo)
    if idx is None:
        return None
    return await page.evaluate(_JS_STATO_CONTENITORE, idx)


async def main():
    minuti = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    t_inizio = time.time()
    richieste: list[dict] = []
    eventi_pagina: list[dict] = []
    reset_trovati: list[dict] = []

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
        if acct is None:
            log(f"account {ACCOUNT} non trovato")
            return

        session = BrowserSession(acct.id)
        await session.open()
        try:
            ctx = session.context
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            def _registra(url: str, status) -> None:
                if not _INBOX_ENDPOINT.search(url):
                    return
                richieste.append({"t": round(time.time() - t_inizio, 1), "url": url, "status": status})
                del richieste[:-500]

            page.on("response", lambda resp: _registra(resp.url, resp.status))
            page.on("requestfailed", lambda req: _registra(req.url, "failed"))

            await page.goto("https://www.instagram.com/direct/inbox/", wait_until="commit", timeout=60000)
            await page.wait_for_timeout(8000)

            async def _evento_pagina(tipo: str) -> None:
                eventi_pagina.append({"t": round(time.time() - t_inizio, 1), "tipo": tipo})

            await page.expose_function("_probeEvento", _evento_pagina)
            await page.evaluate("""
                () => {
                    document.addEventListener('visibilitychange',
                        () => window._probeEvento('visibilitychange:' + document.visibilityState));
                    window.addEventListener('pagehide', () => window._probeEvento('pagehide'));
                    window.addEventListener('freeze', () => window._probeEvento('freeze'));
                }
            """)

            altezza_prec = None
            scadenza = time.time() + minuti * 60
            log(f"sonda avviata, {minuti} min, soglia crollo {CROLLO_SOGLIA_PX}px")
            while time.time() < scadenza:
                await scorri(page)
                stato = await stato_lista(page)
                altezza = stato["altezza"] if stato else None
                dati_righe = await page.evaluate(_JS_RIGHE, 30)
                righe_ora = len((dati_righe or {}).get("righe") or [])

                if altezza is not None and altezza_prec is not None and altezza_prec - altezza > CROLLO_SOGLIA_PX:
                    t_rel = round(time.time() - t_inizio, 1)
                    log(f"!! RESET: altezza {altezza_prec} -> {altezza} (t={t_rel}s)")
                    reset_trovati.append({
                        "quando": datetime.now().isoformat(timespec="seconds"),
                        "t_relativo": t_rel,
                        "altezza_prima": altezza_prec, "altezza_dopo": altezza,
                        "righe_dom_dopo": righe_ora,
                        "visibility_state": await page.evaluate("() => document.visibilityState"),
                        "url_pagina": page.url,
                        "ultime_richieste": richieste[-30:],
                        "eventi_pagina_ultimi_30s": [e for e in eventi_pagina if e["t"] >= t_rel - 30],
                    })

                altezza_prec = altezza
                await page.wait_for_timeout(2000)

            log(f"sonda finita: {len(reset_trovati)} reset trovati")
        finally:
            await session.close()
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump({
                    "reset_trovati": reset_trovati,
                    "richieste_totali_osservate": len(richieste),
                    "eventi_pagina": eventi_pagina,
                }, f, ensure_ascii=False, indent=2, default=str)
            log(f"rapporto salvato in {OUT}")


asyncio.run(main())
