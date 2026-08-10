# backend/scripts/probe_inbox_web_apri_riga.py
"""Verifica dal vivo del fix di geometria: le funzioni VERE contro il DOM VERO.

Non simula niente: chiama `leggi_righe_visibili`, `apri_riga` e `scorri` del
motore, cosi' come le chiama `run_inbox_browser_list`. Quello che deve uscire:

- righe lette senza placeholder fantasma (niente "nome_atteso mancante");
- per una riga gia' letta, `apri_riga` ritorna uno USERNAME (prima del fix
  tornava sempre None: nome e href stavano sotto la soglia 660 cablata);
- `scorri` fa crescere lo scrollHeight della LISTA anche a chat aperta (prima
  del fix scrollava il pannello della conversazione e la lista restava ferma).

Nessuna scrittura su DB, nessuna API mobile: apre chat gia' lette, come farebbe
una lettura umana.

Uso (dal folder backend, con la sessione del bot ferma):
    ./venv/Scripts/python.exe scripts/probe_inbox_web_apri_riga.py primero_adv3
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.services.inbox_browser.pagina import (
    apri_riga, leggi_righe_visibili, scorri,
)

LINGUA = "it"


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


async def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "primero_adv3"
    quante_aprire = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == username))).scalar_one_or_none()
    if acct is None:
        print(f"[X] account {username} non trovato")
        return

    session = BrowserSession(acct.id)
    await session.open()
    esiti = []
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.instagram.com/direct/inbox/",
                        wait_until="commit", timeout=60000)
        await page.wait_for_timeout(8000)

        righe = await leggi_righe_visibili(page, LINGUA)
        print(f"\n=== RIGHE LETTE: {len(righe)} ===")
        for r in righe:
            print(f"  [{r.indice:>2}] non_letta={str(r.non_letta):<5} nostro={str(r.ultimo_nostro):<5} "
                  f"| {p(r.nome)}")
        vuote = [r for r in righe if not r.nome]
        print(f"  righe senza nome (placeholder fantasma): {len(vuote)}  <- deve essere 0")

        candidate = [r for r in righe if r.nome and not r.non_letta][:quante_aprire]
        print(f"\n=== APERTURA DI {len(candidate)} RIGHE GIA' LETTE ===")
        for r in candidate:
            got = await apri_riga(page, r.indice, r.nome, LINGUA, username)
            esiti.append((r.nome, got))
            print(f"  {p(r.nome):<28} -> username={p(got)}")

            await page.wait_for_timeout(1500)

        print("\n=== SCROLL DELLA LISTA (5 giri, a chat aperta) ===")
        from app.services.inbox_browser.pagina import (
            _JS_CANDIDATI, _JS_STATO_CONTENITORE, bordo_colonne, scegli_contenitore,
        )
        from app.services.inbox_browser.pagina import _JS_RIGHE as _JS_RIGHE_MOTORE
        for giro in range(5):
            cand = await page.evaluate(_JS_CANDIDATI)
            bordo = bordo_colonne((await page.evaluate(_JS_RIGHE_MOTORE, 30))["righe"])
            idx = scegli_contenitore(cand, bordo)
            prima = await page.evaluate(_JS_STATO_CONTENITORE, idx) if idx is not None else None
            stato = await scorri(page)
            dopo = await page.evaluate(_JS_STATO_CONTENITORE, idx) if idx is not None else None
            print(f"  giro {giro}: idx={idx} scrollTop {prima and prima['top']} -> {dopo and dopo['top']} "
                  f"| altezza {prima and prima['altezza']} -> {stato.altezza} alFondo={stato.al_fondo}")
            await page.wait_for_timeout(800)

    finally:
        await session.close()
        riusciti = sum(1 for _, u in esiti if u)
        print(f"\n[ESITO] aperture riuscite: {riusciti}/{len(esiti)}")
        print("[OK] chiuso — nessuna scrittura su DB")


asyncio.run(main())
