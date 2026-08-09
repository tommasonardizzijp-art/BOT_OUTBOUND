"""Probe: lo username dei contatti e' recuperabile dall'inbox WEB senza API mobile?

Contesto: il listing inbox via browser fu rimosso perche' la LISTA delle chat su
instagram.com mostra solo il nome visualizzato ("Bruzzo Abbigliamento"), non
l'@username ne' il pk. Ma aprendo un thread l'header mostra anche lo username
(bruzzoabbigliamento) -> quel dato arriva da qualche parte. Questo probe accerta
DA DOVE: payload di rete della lista, payload del thread, o solo DOM.

Read-only e supervisionato: naviga, ascolta le response, apre UN thread, legge.
Nessun invio, nessuna scrittura su DB, nessuna chiamata alla private API mobile.

Uso (dal folder backend):
    ./venv/Scripts/python.exe scripts/probe_inbox_web_username.py "<account_username>"
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
INTERESTING = re.compile(r"(direct_v2|direct/|graphql|api/v1)", re.I)


def _walk(obj, path="$"):
    """Genera (path, dict) per ogni dict annidato."""
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            yield from _walk(v, f"{path}[{i}]")


def analizza(payloads, etichetta):
    """Cerca coppie username/pk dentro i payload raccolti."""
    print(f"\n{'=' * 66}\n{etichetta}\n{'=' * 66}")
    if not payloads:
        print("  nessuna response JSON catturata")
        return set()
    trovati = set()
    for url, body in payloads:
        try:
            data = json.loads(body)
        except Exception:
            continue
        hits = []
        for path, d in _walk(data):
            u = d.get("username")
            if isinstance(u, str) and u.strip():
                pk = d.get("pk") or d.get("id") or d.get("pk_id")
                hits.append((path, u, pk))
                trovati.add(u)
        if hits:
            print(f"\n  [HIT] {url[:100]}")
            print(f"        {len(hits)} oggetti con 'username'. Primi 5:")
            for path, u, pk in hits[:5]:
                print(f"          {u:<28} pk={pk}   <- {path[:70]}")
        else:
            print(f"  [   ] {url[:100]}  (nessun 'username')")
    return trovati


async def main():
    if len(sys.argv) < 2:
        print('Uso: python scripts/probe_inbox_web_username.py "<account_username>"')
        return
    username = sys.argv[1]

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == username)
        )).scalar_one_or_none()
    if acct is None:
        print(f"[X] account {username!r} non trovato in DB")
        return

    print(f"[..] apro il browser col profilo di {acct.username} (status={acct.status.value})")
    session = BrowserSession(acct.id)
    try:
        await session.open()
    except Exception as e:
        print(f"[X] apertura browser fallita ({type(e).__name__}): {e}")
        print("     -> se e' AccountBrowserBusy: un altro processo tiene il profilo.")
        return

    lista_payloads = []
    thread_payloads = []
    fase = {"corrente": "lista"}
    pendenti = []   # task di lettura body: vanno attesi, o si perdono le response

    async def _leggi(resp, target):
        try:
            ctype = (await resp.header_value("content-type")) or ""
            if "json" not in ctype.lower():
                return
            body = await resp.text()
        except Exception:
            return
        target.append((resp.url, body))

    def on_response(resp):
        if not INTERESTING.search(resp.url):
            return
        target = lista_payloads if fase["corrente"] == "lista" else thread_payloads
        pendenti.append(asyncio.create_task(_leggi(resp, target)))

    async def drena():
        if pendenti:
            await asyncio.gather(*pendenti, return_exceptions=True)
            pendenti.clear()

    try:
        ctx = session.context
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", on_response)

        print("[..] vado su instagram.com/direct/inbox/ ...")
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
        await page.wait_for_timeout(12000)
        await drena()

        titolo = await page.title()
        print(f"     titolo pagina: {titolo!r}   url: {page.url}")
        if "login" in page.url.lower():
            print("[X] la sessione web non e' loggata: il probe si ferma qui (niente login automatico).")
            return

        da_lista = analizza(lista_payloads, "FASE 1 — payload caricati dalla LISTA chat (nessun thread aperto)")

        # ── apre UN solo thread ────────────────────────────────────────────
        fase["corrente"] = "thread"
        print("\n[..] apro UNA chat (la prima della lista) ...")
        # I selettori semantici (role=listitem) non esistono sulla lista DM di IG:
        # le righe sono div cliccabili nella colonna sinistra. Le individuiamo per
        # geometria (x < 660 = colonna chat) prendendo il contenitore piu' esterno
        # che contiene l'anteprima del messaggio.
        aperto = False
        try:
            box = await page.evaluate_handle(
                """() => {
                    const cand = [...document.querySelectorAll('div[role="button"], div[tabindex="0"], a')]
                      .filter(e => { const r = e.getBoundingClientRect();
                        return r.left < 660 && r.top > 200 && r.height > 50 && r.height < 130 && r.width > 250; });
                    return cand[0] || null;
                }"""
            )
            el = box.as_element()
            if el:
                await el.click(timeout=6000)
                await page.wait_for_timeout(9000)
                aperto = "/direct/t/" in page.url
        except Exception as e:
            print(f"     click fallito: {type(e).__name__}: {e}")
        await drena()
        print(f"     url dopo il click: {page.url}   (thread aperto: {aperto})")

        analizza(thread_payloads, "FASE 2 — payload caricati APRENDO il thread")

        # ── cosa si vede nel DOM dell'header ──────────────────────────────
        print(f"\n{'=' * 66}\nFASE 3 — DOM del thread aperto\n{'=' * 66}")
        try:
            info = await page.evaluate(
                """() => {
                    const href = [...document.querySelectorAll('a[href^="/"]')]
                      .map(e => e.getAttribute('href'))
                      .filter(h => h && h.split('/').filter(Boolean).length === 1);
                    // header del thread: i primi nodi di testo in alto a destra
                    const testi = [...document.querySelectorAll('span, div')]
                      .filter(e => { const r = e.getBoundingClientRect();
                        return r.left > 660 && r.top < 130 && r.height > 8 && e.children.length === 0
                               && e.textContent.trim().length > 1; })
                      .map(e => e.textContent.trim());
                    return {href: [...new Set(href)].slice(0, 12),
                            header: [...new Set(testi)].slice(0, 10)};
                }"""
            )
            print(f"  href a profilo nella pagina : {info['href']}")
            print(f"  testi nell'header del thread: {info['header']}")
        except Exception as e:
            print(f"  lettura DOM fallita: {e}")

        os.makedirs(OUT_DIR, exist_ok=True)
        dump = os.path.join(OUT_DIR, "probe_inbox_web_username.json")
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump({
                "lista": [{"url": u, "body": b[:200000]} for u, b in lista_payloads],
                "thread": [{"url": u, "body": b[:200000]} for u, b in thread_payloads],
            }, fh, ensure_ascii=False)
        print(f"\n[OK] dump completo dei payload: {dump}")
        print(f"     username distinti visti nella sola FASE 1: {len(da_lista)}")

    finally:
        await session.close()
        print("[OK] browser chiuso.")


asyncio.run(main())
