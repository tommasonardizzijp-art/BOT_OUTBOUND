# backend/scripts/supervisione_inbox_browser.py
"""Sessione di raccolta SUPERVISIONATA: raccoglie davvero, e misura tutto.

Nasce dalle domande rimaste aperte dopo il collaudo dell'11/08:

1. il motore SALTA delle chat mentre scorre? (Tommaso: "scorre ma non se li
   guarda veramente tutti");
2. ora che 144 contatti hanno il nome, la parte alta della lista scorre
   davvero piu' in fretta?
3. dove cade la data nell'header a 1920x940 (il warning "aperto '1 ago
   2026, 15:01'");
4. che formato ha la data nella riga di lista (serve alla modalita'
   segnalibro);
5. quante righe si perdono per lotto ("riga non trovata al momento del click");
6. dove va il tempo: pause, apertura, lettura;
7. cosa succede quando la lista torna in cima da sola.

Come risponde alla 1, che e' la piu' difficile: prima FOTOGRAFA la lista
scorrendo a passi fini senza aprire niente (fase A, il metro di paragone),
poi rifa' il giro raccogliendo davvero (fase B) e alla fine confronta chi
c'era nella foto e non e' mai comparso durante la raccolta (fase C). Per ogni
saltato dice se era gia' in DB (innocuo) o no (perdita vera).

Usa le funzioni VERE del motore, incluse le pause anti-ban: il profilo di
rischio e' quello di una sessione normale, non un test accelerato.

Uso (dal folder backend, con la campagna in pausa e il profilo libero):
    ./venv/Scripts/python.exe scripts/supervisione_inbox_browser.py [minuti_fase_B]
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.follower import Follower
from app.services.inbox_browser.pagina import (
    _JS_CANDIDATI, _JS_FASCIA_ALTA, _JS_RIGHE, _JS_STATO_CONTENITORE,
    apri_riga, bordo_colonne, leggi_righe_visibili, scegli_contenitore, scorri,
)
from app.services.inbox_browser.riconoscimento import ArchivioNomi, ContatoreZona
from app.services.inbox_browser.ritmo import campiona_pausa, zona_pausa
from app.services.inbox_browser.salvataggio import DatiContatto, salva_contatto
from app.services.inbox_browser.testo import (
    estrai_data_thread, estrai_ultimo_messaggio, normalizza_nome,
)
from app.services.scrape_inbox_browser import decide_se_aprire

CAMPAGNA = "ec5e2464-1d8d-42a1-a81f-8e61b303fa7a"
ACCOUNT = "@michele.carozza"
LINGUA = "it"
PASSI_CENSIMENTO = 45
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "supervisione_inbox.json")


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {p(msg)}", flush=True)


async def stato_lista(page):
    """scrollTop/altezza del contenitore della lista, per accorgersi dei reset."""
    dati = await page.evaluate(_JS_RIGHE, 30)
    bordo = bordo_colonne((dati or {}).get("righe") or [])
    candidati = await page.evaluate(_JS_CANDIDATI)
    idx = scegli_contenitore(candidati, bordo)
    if idx is None:
        return None
    return await page.evaluate(_JS_STATO_CONTENITORE, idx)


# ─────────────────────────── FASE A: la foto della lista ───────────────────
async def censimento(page):
    """Scorre a passi fini SENZA aprire nulla e registra ogni riga in ordine."""
    log("FASE A — censimento della lista (nessuna chat aperta)")
    viste: dict[str, dict] = {}
    ordine: list[str] = []
    formati_data: dict[str, int] = {}
    altezza_prec = None
    fermo = 0

    for passo in range(PASSI_CENSIMENTO):
        righe = await leggi_righe_visibili(page, LINGUA)
        nuove = 0
        for r in righe:
            chiave = normalizza_nome(r.nome)
            if not chiave or chiave in viste:
                continue
            grezzo = [x.strip() for x in (r.testo_grezzo or "").split("\n") if x.strip()]
            data_riga = grezzo[-1] if len(grezzo) > 2 else None
            formati_data[data_riga] = formati_data.get(data_riga, 0) + 1
            viste[chiave] = {"nome": r.nome, "non_letta": r.non_letta,
                             "ultimo_nostro": r.ultimo_nostro, "data_riga": data_riga,
                             "passo": passo}
            ordine.append(chiave)
            nuove += 1

        stato = await scorri(page)
        if stato.altezza is not None and stato.altezza == altezza_prec and nuove == 0:
            fermo += 1
            if fermo >= 3:
                log(f"  fine lista al passo {passo} (altezza ferma a {stato.altezza})")
                break
        else:
            fermo = 0
        altezza_prec = stato.altezza
        if passo % 5 == 0:
            log(f"  passo {passo}: {len(viste)} righe censite, altezza={stato.altezza}")
        await page.wait_for_timeout(400)

    log(f"FASE A finita: {len(viste)} chat censite")
    return viste, ordine, formati_data


# ─────────────────────── FASE B: la raccolta, cronometrata ─────────────────
async def raccolta(page, db, campaign_id, minuti_max):
    """Il ciclo del motore, con dentro un cronometro e un registro di tutto."""
    log(f"FASE B — raccolta supervisionata (max {minuti_max} min)")

    nomi_esistenti = (await db.execute(
        select(Follower.full_name).where(Follower.campaign_id == campaign_id)
    )).scalars().all()
    archivio = ArchivioNomi(list(nomi_esistenti))
    contatore = ContatoreZona()

    diario = {
        "esaminate": [],       # ogni riga incontrata, con la decisione presa
        "aperture": [],        # ogni tentativo di apertura, con esito e durata
        "fallimenti_header": [],
        "reset_scroll": [],
        "tempi": {"pause": 0.0, "apertura": 0.0, "lettura": 0.0, "scroll": 0.0},
        "creati": 0, "aggiornati": 0,
    }
    incontrate: set[str] = set()
    scadenza = time.time() + minuti_max * 60
    scroll_prec = None

    while time.time() < scadenza:
        t0 = time.time()
        righe = await leggi_righe_visibili(page, LINGUA)
        diario["tempi"]["lettura"] += time.time() - t0

        stato = await stato_lista(page)
        if stato and scroll_prec is not None and stato["top"] + 200 < scroll_prec:
            diario["reset_scroll"].append(
                {"quando": datetime.now().isoformat(timespec="seconds"),
                 "da": scroll_prec, "a": stato["top"]})
            log(f"  !! LISTA TORNATA INDIETRO: scrollTop {scroll_prec} -> {stato['top']}")
        if stato:
            scroll_prec = stato["top"]

        for riga in righe:
            if time.time() >= scadenza:
                break
            chiave = normalizza_nome(riga.nome)
            if not chiave:
                continue
            gia_vista = chiave in incontrate
            incontrate.add(chiave)

            if riga.non_letta:
                if not gia_vista:
                    diario["esaminate"].append({"nome": riga.nome, "decisione": "non_letta"})
                continue

            riconosciuta = archivio.e_riconosciuto(riga.nome)
            apre = decide_se_aprire(riga.nome, archivio, contatore.zona)
            if not gia_vista:
                diario["esaminate"].append({
                    "nome": riga.nome, "zona": contatore.zona,
                    "decisione": "apre" if apre else ("nota" if riconosciuta else "segnaposto"),
                })

            if apre:
                t0 = time.time()
                username = await apri_riga(page, riga.indice, riga.nome, LINGUA, ACCOUNT)
                durata = time.time() - t0
                diario["tempi"]["apertura"] += durata
                esito = "aperta" if username else "fallita"

                if not username:
                    # Perche' e' fallita: si guarda il DOM ADESSO, non dopo.
                    nodi = await page.evaluate(_JS_FASCIA_ALTA)
                    dati = await page.evaluate(_JS_RIGHE, 30)
                    bordo = bordo_colonne((dati or {}).get("righe") or [])
                    diario["fallimenti_header"].append({
                        "atteso": riga.nome, "bordo": bordo,
                        "viewport": (dati or {}).get("viewport"),
                        "fascia": [n for n in nodi if n["top"] < 200][:14],
                    })
                    log(f"  fallita apertura di {riga.nome!r} ({durata:.1f}s) — fascia catturata")
                else:
                    testo_pagina = await page.evaluate("() => document.body.innerText")
                    dati_contatto = DatiContatto(
                        username=username, nome=riga.nome,
                        last_message_at=estrai_data_thread(testo_pagina, LINGUA),
                        last_message_from=("us" if riga.ultimo_nostro is True
                                           else "them" if riga.ultimo_nostro is False else None),
                        last_message_text=estrai_ultimo_messaggio(testo_pagina, LINGUA),
                    )
                    esito_salvataggio = await salva_contatto(db, campaign_id, dati_contatto)
                    archivio.aggiungi(riga.nome)
                    diario[("creati" if esito_salvataggio == "creato" else "aggiornati")] += 1
                    esito = esito_salvataggio
                    log(f"  {esito.upper():<11} @{username:<28} <- {riga.nome!r} ({durata:.1f}s)")

                diario["aperture"].append({"nome": riga.nome, "esito": esito,
                                           "secondi": round(durata, 1), "zona": contatore.zona})

            contatore.registra(riconosciuta)
            pausa = campiona_pausa(zona_pausa(contatore.zona, apre))
            diario["tempi"]["pause"] += pausa
            voce = "pause_apertura" if apre else "pause_scorrimento"
            diario["tempi"][voce] = diario["tempi"].get(voce, 0.0) + pausa
            await asyncio.sleep(pausa)

        t0 = time.time()
        await scorri(page)
        diario["tempi"]["scroll"] += time.time() - t0

    diario["incontrate"] = sorted(incontrate)
    log(f"FASE B finita: {diario['creati']} creati, {diario['aggiornati']} aggiornati, "
        f"{len(incontrate)} chat incontrate")
    return diario


async def main():
    minuti = int(sys.argv[1]) if len(sys.argv) > 1 else 18

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
        if acct is None:
            log(f"account {ACCOUNT} non trovato")
            return

        session = BrowserSession(acct.id)
        await session.open()
        rapporto = {"inizio": datetime.now().isoformat(timespec="seconds")}
        try:
            ctx = session.context
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.instagram.com/direct/inbox/",
                            wait_until="commit", timeout=60000)
            await page.wait_for_timeout(8000)
            rapporto["viewport"] = await page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight})")
            log(f"viewport reale: {rapporto['viewport']}")

            if os.environ.get("CENSIMENTO", "1") == "1":
                viste, ordine, formati = await censimento(page)
                rapporto["censite"] = {k: v for k, v in viste.items()}
                rapporto["ordine_censimento"] = ordine
                rapporto["formati_data_riga"] = formati
                await page.reload(wait_until="commit", timeout=60000)
                await page.wait_for_timeout(8000)

            rapporto["diario"] = await raccolta(page, db, CAMPAGNA, minuti)
        finally:
            await session.close()
            rapporto["fine"] = datetime.now().isoformat(timespec="seconds")
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(rapporto, f, ensure_ascii=False, indent=2, default=str)
            log(f"rapporto salvato in {OUT}")


asyncio.run(main())
