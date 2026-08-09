"""Probe: quanto si puo' andare veloci ad aprire le chat senza perdere il dato?

Due misure, su poche chat e con pacing umano:
  A) i nomi visibili nella LISTA si leggono senza aprire niente? (serve per
     ritrovare il segno tra una sessione e l'altra riconoscendo chi e' gia' noto,
     invece di contare le posizioni: la lista si riordina a ogni DM in entrata)
  B) dal click sulla chat, quanti millisecondi passano prima che lo username
     dell'interlocutore sia effettivamente leggibile? E' il tetto reale alla
     velocita': piu' veloci di cosi' e si passa oltre prima che il dato esista.

Read-only: apre chat e legge. Nessun invio, nessuna scrittura su DB.
NOTA: aprire una chat la marca come letta (scelta accettata).

Uso (dal folder backend):
    ./venv/Scripts/python.exe scripts/probe_inbox_web_tempi.py "<account>" [n_chat]
"""
import asyncio
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

# Quanto aspettiamo al massimo che il dato compaia prima di dichiarare "perso".
TIMEOUT_DATO_MS = 8000
# Ritmo umano tra un'apertura e la successiva durante la MISURA.
PAUSA_MIN_S, PAUSA_MAX_S = 1.5, 3.5

JS_NOMI_LISTA = """() => {
    // Righe della colonna sinistra: le prendiamo per geometria, non per classi
    // (le classi di IG cambiano di continuo).
    const righe = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
      .filter(e => { const r = e.getBoundingClientRect();
        return r.left < 660 && r.top > 200 && r.height > 50 && r.height < 130 && r.width > 250; });
    return righe.map(e => {
        const t = e.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
        return t[0] || null;
    }).filter(Boolean);
}"""

JS_USERNAME_THREAD = """() => {
    // L'header del thread mostra "nomeutente - Instagram" e c'e' un link al
    // profilo: prendiamo il link, e' il dato pulito.
    const link = [...document.querySelectorAll('a[href^="/"]')]
      .map(e => e.getAttribute('href'))
      .filter(h => h && h.split('/').filter(Boolean).length === 1)
      .map(h => h.replace(/\\//g, ''));
    const propri = new Set(['reels', 'explore', 'direct']);
    const cand = link.filter(u => !propri.has(u));
    return cand.length ? cand[cand.length - 1] : null;
}"""


def _stampabile(s) -> str:
    """La console Windows e' cp1252: le emoji nei nomi profilo la fanno crashare."""
    return str(s).encode("ascii", "replace").decode("ascii")


async def main():
    if len(sys.argv) < 2:
        print('Uso: python scripts/probe_inbox_web_tempi.py "<account>" [n_chat]')
        return
    username = sys.argv[1]
    n_chat = int(sys.argv[2]) if len(sys.argv) > 2 else 6

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == username)
        )).scalar_one_or_none()
    if acct is None:
        print(f"[X] account {username!r} non trovato in DB")
        return

    print(f"[..] apro il browser col profilo di {acct.username}")
    session = BrowserSession(acct.id)
    try:
        await session.open()
    except Exception as e:
        print(f"[X] apertura browser fallita ({type(e).__name__}): {e}")
        return

    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
        await page.wait_for_timeout(10000)
        if "login" in page.url.lower():
            print("[X] sessione web non loggata, mi fermo.")
            return

        # ── A) i nomi si leggono dalla lista, senza aprire? ────────────────
        nomi = await page.evaluate(JS_NOMI_LISTA)
        print(f"\n{'=' * 66}\nA) NOMI LEGGIBILI DALLA LISTA (nessuna chat aperta)\n{'=' * 66}")
        print(f"  righe lette in una schermata: {len(nomi)}")
        for n in nomi[:8]:
            print(f"    - {_stampabile(n)}")

        # ── B) quanto ci mette il dato a comparire dopo il click ──────────
        print(f"\n{'=' * 66}\nB) TEMPO DAL CLICK ALLA COMPARSA DELLO USERNAME\n{'=' * 66}")
        tempi, persi, catturati = [], 0, []
        for i in range(n_chat):
            righe = await page.evaluate_handle(
                """(idx) => {
                    const c = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
                      .filter(e => { const r = e.getBoundingClientRect();
                        return r.left < 660 && r.top > 200 && r.height > 50 && r.height < 130 && r.width > 250; });
                    return c[idx] || null;
                }""",
                i,
            )
            el = righe.as_element()
            if el is None:
                break
            prima = await page.evaluate(JS_USERNAME_THREAD)
            t0 = asyncio.get_event_loop().time()
            try:
                await el.click(timeout=5000)
            except Exception:
                continue

            # polling fitto: cerchiamo il primo istante in cui il dato esiste
            trovato, atteso = None, 0
            while atteso < TIMEOUT_DATO_MS:
                u = await page.evaluate(JS_USERNAME_THREAD)
                if u and u != prima and u != acct.username.lstrip("@"):
                    trovato = u
                    break
                await page.wait_for_timeout(50)
                atteso = int((asyncio.get_event_loop().time() - t0) * 1000)
            ms = int((asyncio.get_event_loop().time() - t0) * 1000)

            if trovato:
                tempi.append(ms)
                catturati.append(trovato)
                print(f"  chat {i + 1}: {ms:>5} ms   -> {_stampabile(trovato)}")
            else:
                persi += 1
                print(f"  chat {i + 1}: PERSO (oltre {TIMEOUT_DATO_MS} ms)")

            await asyncio.sleep(random.uniform(PAUSA_MIN_S, PAUSA_MAX_S))

        print(f"\n{'-' * 66}")
        if tempi:
            print(f"  campioni      : {len(tempi)}   persi: {persi}")
            print(f"  minimo        : {min(tempi)} ms")
            print(f"  mediana       : {int(statistics.median(tempi))} ms")
            print(f"  massimo       : {max(tempi)} ms")
            sicuro = max(tempi) / 1000 + 0.5
            print(f"\n  => attesa prudente per non perdere dati: ~{sicuro:.1f}s a chat")
            print(f"  => con quel ritmo: ~{int(3600 / (sicuro + 1.5))} chat/ora (pausa umana 1.5s inclusa)")
            print(f"  username distinti catturati: {len(set(catturati))}")
        else:
            print("  nessun tempo misurato")

    finally:
        await session.close()
        print("[OK] browser chiuso.")


asyncio.run(main())
