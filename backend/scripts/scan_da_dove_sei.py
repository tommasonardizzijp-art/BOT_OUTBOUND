"""Apre il browser, aspetta che l'umano scorra la lista chat, poi scansiona DA LI'.

    cd "D:\\BOT OUTBOUND\\backend"
    python -m scripts.scan_da_dove_sei <number_id> [--segnale PERCORSO]

Poi, quando la lista e' al punto giusto, si crea il file segnale che lo
script sta aspettando (il percorso viene stampato all'avvio):

    echo vai > data\\VAI.txt

PERCHE' ESISTE, invece di usare la scansione normale dalla UI
-------------------------------------------------------------
`esegui_discover_run` apre il browser e fa `page.goto(WHATSAPP_WEB_URL)`,
che riporta la sidebar in cima. Qualunque scorrimento fatto prima viene
buttato via. Su una rubrica grande questo costa carissimo: tornare al punto
d'arresto su 900 chat sono ~2,7 ore di solo scorrimento (misurato: 2,8
righe/minuto), anche saltando le chat gia' note.

Qui la navigazione avviene UNA volta, poi si cede il controllo all'umano:
si porta la lista a mano dove serve -- un minuto -- e `_esegui_scan` lavora
sulla pagina nello stato in cui la trova.

COSA PATCHA, E PERCHE'
----------------------
1. `_decidi_riga`: salta senza pause le chat gia' note (per titolo o per
   hmac del numero). E' il "riprendi da dove eri" vero e proprio.
2. `campiona_pausa`: pause di scorrimento a 50 ms invece del ritmo umano.
   Le righe saltate non aprono nulla, quindi non c'e' niente da mascherare.
3. `leggi_sincronizzazione`: torna "ignota" SENZA toccare la pagina. Non e'
   una bugia e non indebolisce la guardia: `_SEL_IMPOSTAZIONI` oggi non
   matcha su questo WhatsApp Web (verificato dal vivo il 15/08, due sessioni
   distinte), quindi la lettura vera direbbe comunque "ignota" -- ma per
   scoprirlo aprirebbe il pannello Impostazioni, e richiuderlo puo' far
   ricaricare la pagina azzerando lo scorrimento posizionato a mano. Lo
   stato "ignota" finisce comunque in `wa_discover_runs.sync_stato` e la UI
   lo mostra come primo indiziato se la raccolta risulta corta.

NON patcha piu' `lista_utilizzabile`: il difetto per cui usciva sulla PRIMA
riga candidata (che da meta' lista in giu' sta dietro l'intestazione, e
faceva rifiutare lo scan con 'sidebar_coperta') e' corretto a monte, in
`sincronizzazione.py`, dalla PR #85.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SEGNALE_DEFAULT = Path("data") / "VAI.txt"
ATTESA_MAX_MIN = 30
SOGLIA_STALLO = 15
ATTESE_PANNELLO = (1.0, 1.5, 2.0, 2.5, 4.0, 5.0, 5.0)


# Diagnostica: quando la lista risulta coperta, dire CHI la copre. Senza
# questo si scopriva il rifiuto solo a browser gia' chiuso, cioe' quando non
# si poteva piu' guardare.
_JS_CHI_COPRE = """() => {
    const pane = document.querySelector('#pane-side');
    if (!pane) return 'nessun #pane-side: la lista chat non e in pagina';
    const righe = pane.querySelectorAll("[role='row']");
    if (!righe.length) return 'lista presente ma senza righe renderizzate';
    for (const r of righe) {
        const box = r.getBoundingClientRect();
        if (box.width < 10 || box.height < 10) continue;
        if (box.top < 0 || box.top > window.innerHeight - 20) continue;
        const sopra = document.elementFromPoint(
            box.left + box.width / 2, box.top + box.height / 2);
        if (!sopra) return 'elementFromPoint non torna nulla (finestra coperta o fuori schermo?)';
        if (pane.contains(sopra)) return 'ok, la riga e cliccabile';
        const d = e => e ? `<${e.tagName.toLowerCase()}`
            + (e.id ? ` id=${e.id}` : '')
            + (e.getAttribute('data-testid') ? ` testid=${e.getAttribute('data-testid')}` : '')
            + (e.className && typeof e.className === 'string'
                 ? ` class="${e.className.slice(0, 60)}"` : '')
            + '>' : 'null';
        return 'sopra la riga c e: ' + d(sopra)
            + ' | padre: ' + d(sopra.parentElement)
            + ' | nonno: ' + d(sopra.parentElement && sopra.parentElement.parentElement)
            + ` | finestra ${window.innerWidth}x${window.innerHeight}`;
    }
    return 'nessuna riga dentro il viewport (lista scrollata fuori? finestra troppo bassa?)';
}"""


async def _chi_copre(page) -> str:
    try:
        return await page.evaluate(_JS_CHI_COPRE)
    except Exception as exc:  # noqa: BLE001 -- e' diagnostica, non deve mai
        return f"diagnostica fallita: {type(exc).__name__}: {exc}"


def _argomenti() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("number_id", help="id del numero WhatsApp da scansionare")
    p.add_argument("--segnale", default=str(SEGNALE_DEFAULT),
                   help=f"file che fa partire lo scan (default: {SEGNALE_DEFAULT})")
    p.add_argument("--attesa-max-min", type=int, default=ATTESA_MAX_MIN,
                   help="quanto aspettare l'umano prima di arrendersi")
    return p.parse_args()


async def main() -> int:
    args = _argomenti()
    number_id = args.number_id
    segnale = Path(args.segnale).resolve()
    segnale.parent.mkdir(parents=True, exist_ok=True)

    from sqlalchemy import func, select

    from app.browser.whatsapp_page import WhatsAppWebPage
    from app.database import AsyncSessionLocal
    from app.models.wa import WaDiscoveredChat, WaNumber
    from app.services import wa_discover_run as motore
    from app.services import wa_discover_runs, wa_profile_lock
    from app.services.wa_discover import classifica, pannello
    from app.services.wa_discover import sidebar
    from app.services.wa_discover.sincronizzazione import LetturaSync
    from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser
    from app.utils.phone_pseudonym import hmac_phone

    if segnale.exists():
        segnale.unlink()

    async with AsyncSessionLocal() as db:
        numero = await db.scalar(select(WaNumber).where(WaNumber.id == number_id))
        if numero is None:
            print(f"[scan] numero {number_id} inesistente", file=sys.stderr)
            return 2
        tenant_id, proxy_url, etichetta = numero.tenant_id, numero.proxy_url, numero.label
        coppie = (await db.execute(
            select(WaDiscoveredChat.chat_title, WaDiscoveredChat.phone_hmac).where(
                WaDiscoveredChat.number_id == number_id,
                WaDiscoveredChat.phone_hmac.is_not(None)))).all()
        prima = await db.scalar(select(func.count(WaDiscoveredChat.id)).where(
            WaDiscoveredChat.number_id == number_id)) or 0

    hmac_noti = {h for _, h in coppie}
    titoli_noti = {t for t, _ in coppie if t and "\u2022" not in t}
    print(f"[scan] numero: {etichetta}")
    print(f"[scan] noti: {len(hmac_noti)} numeri, {len(titoli_noti)} titoli in chiaro")
    print(f"[scan] righe in staging: {prima}")

    # --- 1. salto incrementale, senza pausa sulle righe saltate --------------
    originale = motore._decidi_riga
    stato = {"saltate": 0, "aperte": 0}

    async def _decidi(page, grezza):
        titolo = grezza.get("titolo")
        noto = bool(titolo and titolo in titoli_noti)
        if not noto and grezza.get("titolo_e_numero"):
            n = classifica.numero_dal_titolo(titolo)
            noto = n is not None and hmac_phone(n) in hmac_noti
        if noto:
            stato["saltate"] += 1
            if stato["saltate"] % 25 == 0:
                print(f"[scan] {stato['saltate']} note saltate, "
                      f"{stato['aperte']} processate", flush=True)
            return motore.DecisioneRiga(riga=None, ha_aperto=False)
        stato["aperte"] += 1
        if stato["aperte"] % 10 == 0:
            print(f"[scan] {stato['aperte']} NUOVE processate", flush=True)
        return await originale(page, grezza)

    motore._decidi_riga = _decidi

    # --- 2. ritmo: le righe saltate non aprono nulla da mascherare -----------
    campiona_orig = motore.campiona_pausa
    motore.campiona_pausa = lambda z: 0.05 if z == "scorrimento" else campiona_orig(z)
    motore.MAX_SCROLL_SENZA_NUOVE_RIGHE = SOGLIA_STALLO
    pannello._ATTESE_PANNELLO_S = ATTESE_PANNELLO

    # --- 3. gate sync senza toccare la pagina (vedi docstring) ---------------
    async def _sync_senza_toccare(page):
        return LetturaSync(stato="ignota", percentuale=None)

    motore.leggi_sincronizzazione = _sync_senza_toccare

    esito: dict = {}
    async with wa_profile_lock.held(number_id) as lock_token:
        async with _open_wa_browser(number_id, headless=False, proxy_url=proxy_url) as ctx:
            page = await ctx.new_page()
            await page.goto(WHATSAPP_WEB_URL, wait_until="commit", timeout=120_000)
            for attesa in (3, 5, 10, 20):
                await page.wait_for_timeout(attesa * 1000)
                if await page.evaluate("() => !!document.querySelector('#pane-side')"):
                    break
            pom = WhatsAppWebPage(page)
            print(f"\n[scan] sessione: {await pom.session_state()}", flush=True)

            print("\n" + "=" * 66)
            print("  SCORRI LA LISTA CHAT fino all'ultimo contatto gia' estratto")
            print("  e lasciala in CIMA alla parte visibile.")
            print("  Poi crea il file segnale per far partire lo scan:")
            print(f"    {segnale}")
            print("=" * 66 + "\n", flush=True)

            # La guardia si interroga DURANTE l'attesa, e quando dice no si
            # stampa chi c'e' sopra: cosi' si corregge prima di partire, non
            # dopo -- il primo tentativo del 15/08 e' morto scoprendo
            # 'sidebar_coperta' a browser gia' chiuso.
            scaduto = True
            for giro in range(args.attesa_max_min * 12):
                if segnale.exists():
                    if await motore.lista_utilizzabile(page):
                        scaduto = False
                        break
                    print("[!] SEGNALE RICEVUTO ma la lista risulta COPERTA: "
                          "non parto. Dettaglio qui sotto, sistema e riprova.",
                          flush=True)
                    print(f"    {await _chi_copre(page)}", flush=True)
                    segnale.unlink()
                if giro % 6 == 0:
                    righe = await sidebar.scan_sidebar(page)
                    titoli = [r.get("titolo") for r in righe[:3]]
                    ok = await motore.lista_utilizzabile(page)
                    print(f"[attesa {giro * 5}s] lista "
                          f"{'libera' if ok else 'COPERTA'} | in cima: {titoli}",
                          flush=True)
                    if not ok:
                        print(f"    {await _chi_copre(page)}", flush=True)
                await asyncio.sleep(5)

            if scaduto:
                print("[scan] nessun segnale entro il tempo massimo, "
                      "esco senza scansionare")
                return 1

            righe = await sidebar.scan_sidebar(page)
            print(f"\n[scan] VIA. Parto da: {[r.get('titolo') for r in righe[:3]]}",
                  flush=True)

            # La run si registra in wa_discover_runs come tutte le altre: da
            # quando esiste la tabella (migrazione 035) una scansione che non
            # lascia traccia e' una scansione che la UI non sa raccontare.
            # `avviato_da='script'` la distingue nello storico da quelle
            # partite dal bottone.
            async with AsyncSessionLocal() as db:
                run = await wa_discover_runs.apri_run(
                    db, tenant_id=tenant_id, number_id=number_id,
                    avviato_da="script")
                await db.commit()
                run_id = run.id

            try:
                async with AsyncSessionLocal() as db:
                    esito = await motore._esegui_scan(
                        page, db=db, tenant_id=tenant_id, number_id=number_id,
                        lock_token=lock_token)
            except Exception as exc:
                async with AsyncSessionLocal() as db:
                    await wa_discover_runs.chiudi_run(db, run_id, {}, errore=str(exc))
                    await db.commit()
                raise
            else:
                async with AsyncSessionLocal() as db:
                    await wa_discover_runs.chiudi_run(db, run_id, esito)
                    await db.commit()

    async with AsyncSessionLocal() as db:
        dopo = await db.scalar(select(func.count(WaDiscoveredChat.id)).where(
            WaDiscoveredChat.number_id == number_id)) or 0

    print("\n=== ESITO ===")
    for k, v in esito.items():
        print(f"  {k}: {v}")
    print(f"  saltate gia' note: {stato['saltate']}")
    print(f"  nuove processate:  {stato['aperte']}")
    print(f"\n=== STAGING === {prima} -> {dopo}")
    return 0


for _flusso in (sys.stdout, sys.stderr):
    try:
        _flusso.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- console senza reconfigure: si tira avanti
        pass

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
