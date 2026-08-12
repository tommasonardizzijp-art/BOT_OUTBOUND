# backend/scripts/apri_browser_a_mano.py
"""Apre il browser sul profilo di un account e NON fa nient'altro.

Serve quando Tommaso vuole guardare o provare qualcosa a mano: stesso profilo,
stessi cookie, stesso proxy e stesso fingerprint del motore — ma nessuna
automazione sopra. Un solo `goto` all'inbox (la stessa cosa che fa una persona
aprendo la pagina), poi il processo resta fermo ad aspettare: e' quello che
tiene viva la finestra, perche' Playwright chiude il browser quando il processo
che l'ha aperto finisce.

Tenendo il profilo occupato impedisce anche al motore di partirci sopra nel
frattempo (lock del profilo), che e' esattamente quello che si vuole.

    ./venv/Scripts/python.exe scripts/apri_browser_a_mano.py [ore]
"""
import asyncio
import os
import sys

# Stessa riga di cold_ping.py / inspect_device.py: lanciato come script, la
# cartella `backend` non e' sul path e `app` non si importa.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.browser.context_manager import BrowserSession  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models.account import InstagramAccount  # noqa: E402

ACCOUNT = "@michele.carozza"
PAGINA = "https://www.instagram.com/direct/inbox/"


async def main() -> None:
    ore = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == ACCOUNT)
        )).scalar_one_or_none()
    if acct is None:
        print(f"account {ACCOUNT} non trovato")
        return

    sessione = BrowserSession(acct.id, headless=False)
    await sessione.open()
    try:
        ctx = sessione.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(PAGINA, wait_until="commit", timeout=60000)
        print(f"browser aperto su {PAGINA} — nessuna automazione attiva.", flush=True)
        print(f"resta aperto fino a {ore:.1f} ore, o finche' non chiudi la finestra.", flush=True)

        # NON un `sleep` lungo e cieco: chiudendo la finestra a mano il
        # contesto muore ma il processo continuerebbe a dormire, e con lui il
        # lucchetto sul profilo — che si rinnova da solo. Risultato visto dal
        # vivo il 12/08: profilo bloccato per tre ore da uno script il cui
        # browser non esisteva piu'. Si controlla se la finestra e' ancora la',
        # e appena sparisce si esce (il `finally` rilascia il lucchetto).
        scaduto = asyncio.get_event_loop().time() + ore * 3600
        while asyncio.get_event_loop().time() < scaduto:
            await asyncio.sleep(5)
            if page.is_closed() or not ctx.pages:
                print("finestra chiusa a mano: esco e libero il profilo.", flush=True)
                return
    finally:
        await sessione.close()
        print("browser chiuso")


if __name__ == "__main__":
    asyncio.run(main())
