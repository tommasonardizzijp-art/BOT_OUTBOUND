# backend/scripts/probe_inbox_web_requestfailed.py
"""Le richieste fallite sono un segnale usabile per distinguere "fine lista" da
"Instagram piantato"? La spec sospetta di no (rumore puro su una SPA).

Registra TUTTE le requestfailed durante 12 scroll, separando quelle verso gli
endpoint dell'inbox dal resto. Sola lettura, nessuna chat aperta.
"""
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

ACCOUNT = "claudio.abbigliamentovincente"
INBOX_ENDPOINT = re.compile(r"(direct_v2|graphql)", re.I)

JS_SCROLL = """() => {
    let box = null, best = 0;
    for (const e of document.querySelectorAll('div')) {
        const r = e.getBoundingClientRect();
        if (r.left > 700 || r.width < 200 || r.height < 300) continue;
        if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue;
        if (e.scrollHeight > best) { best = e.scrollHeight; box = e; }
    }
    if (!box) return null;
    box.scrollTop += box.clientHeight * 0.7;
    return box.scrollHeight;
}"""


async def main():
    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
    session = BrowserSession(acct.id)
    await session.open()
    falliti = []
    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("requestfailed", lambda r: falliti.append((r.url, r.failure)))
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)
        falliti.clear()   # ignoriamo il caricamento iniziale
        for g in range(12):
            h = await page.evaluate(JS_SCROLL)
            await page.wait_for_timeout(2500)
            print(f"  giro {g + 1}: altezza={h}  falliti finora={len(falliti)}")
        print("-" * 60)
        inbox = [u for u, _ in falliti if INBOX_ENDPOINT.search(u)]
        print(f"  richieste fallite TOTALI      : {len(falliti)}")
        print(f"  di cui verso endpoint inbox   : {len(inbox)}")
        for u, f in falliti[:10]:
            print(f"    {f}  {u[:100]}")
    finally:
        await session.close()
        print("[OK] chiuso")


asyncio.run(main())
