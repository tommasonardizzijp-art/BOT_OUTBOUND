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
    ./venv/Scripts/python.exe scripts/supervisione_inbox_browser.py [segnalibro:0|1]

Fase B chiama il motore vero (run_inbox_browser_list), avvolto con wrapper
strumentati (vedi _instrumenta) — non una copia parallela del suo ciclo. Il
budget di sessione (30-55 min) e' quello vero, interno al motore, casuale.
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
from app.models.campaign import Campaign, CampaignStatus
from app.services.inbox_browser.pagina import (
    _JS_FASCIA_ALTA, _JS_RIGHE, bordo_colonne,
    leggi_righe_visibili as _leggi_per_censimento, scorri,
)
from app.services.inbox_browser.testo import normalizza_nome
import app.services.scrape_inbox_browser as motore

CAMPAGNA = "ec5e2464-1d8d-42a1-a81f-8e61b303fa7a"
ACCOUNT = "@michele.carozza"
LINGUA = "it"
PASSI_CENSIMENTO = 45
# Sopra al budget interno del motore (30-55 min, casuale ad ogni sessione):
# rete di sicurezza esterna, non un timer che guida la raccolta.
TIMEOUT_SICUREZZA_S = 70 * 60
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "supervisione_inbox.json")


def p(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {p(msg)}", flush=True)


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
        righe = await _leggi_per_censimento(page, LINGUA)
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


# ────────────── FASE B: il motore VERO, avvolto per misurare ───────────────
def _instrumenta(diario):
    """Avvolge le funzioni del motore reale con cronometri/contatori.

    Non riscrive il ciclo (rischio di duplicare — e far divergere — la logica
    gia' delicata di run_inbox_browser_list): lo si avvolge. Python risolve le
    chiamate per nome nel namespace del modulo AL MOMENTO della chiamata
    (stesso principio di monkeypatch.setattr nei test), quindi patchare gli
    attributi di `motore` qui sotto e' visibile dentro il ciclo vero. Ritorna
    il callback di ripristino.
    """
    originali = {}

    def _sostituisci(nome_attributo, nuova_funzione):
        originali[nome_attributo] = getattr(motore, nome_attributo)
        setattr(motore, nome_attributo, nuova_funzione)

    leggi_reale = motore.leggi_righe_visibili

    async def leggi_strumentata(*a, **kw):
        t0 = time.time()
        r = await leggi_reale(*a, **kw)
        diario["tempi"]["lettura"] += time.time() - t0
        return r

    _sostituisci("leggi_righe_visibili", leggi_strumentata)

    gia_esaminata_reale = motore.gia_esaminata

    def gia_esaminata_strumentata(*a, **kw):
        r = gia_esaminata_reale(*a, **kw)
        if r:
            diario["righe_ripetute"] += 1
        return r

    _sostituisci("gia_esaminata", gia_esaminata_strumentata)

    riga_da_saltare_reale = motore.riga_da_saltare

    def riga_da_saltare_strumentata(*a, **kw):
        r = riga_da_saltare_reale(*a, **kw)
        if r:
            diario["righe_saltate"] += 1
        return r

    _sostituisci("riga_da_saltare", riga_da_saltare_strumentata)

    apri_riga_reale = motore.apri_riga

    async def apri_riga_strumentata(page, indice, nome_atteso, lingua, account_username=None):
        t0 = time.time()
        username = await apri_riga_reale(page, indice, nome_atteso, lingua, account_username)
        durata = time.time() - t0
        diario["tempi"]["apertura"] += durata
        esito = "aperta" if username else "fallita"
        diario["aperture"].append({"nome": nome_atteso, "esito": esito, "secondi": round(durata, 1)})
        if username:
            log(f"  APERTA      @{username:<28} <- {nome_atteso!r} ({durata:.1f}s)")
        else:
            # Perche' e' fallita: si guarda il DOM ADESSO, non dopo.
            nodi = await page.evaluate(_JS_FASCIA_ALTA)
            dati = await page.evaluate(_JS_RIGHE, 30)
            bordo = bordo_colonne((dati or {}).get("righe") or [])
            diario["fallimenti_header"].append({
                "atteso": nome_atteso, "bordo": bordo,
                "viewport": (dati or {}).get("viewport"),
                "fascia": [n for n in nodi if n["top"] < 200][:14],
            })
            log(f"  fallita apertura di {nome_atteso!r} ({durata:.1f}s) — fascia catturata")
        return username

    _sostituisci("apri_riga", apri_riga_strumentata)

    salva_contatto_reale = motore.salva_contatto

    async def salva_contatto_strumentata(*a, **kw):
        esito = await salva_contatto_reale(*a, **kw)
        diario[("creati" if esito == "creato" else "aggiornati")] += 1
        return esito

    _sostituisci("salva_contatto", salva_contatto_strumentata)

    decidi_fine_lista_reale = motore.decidi_fine_lista

    async def decidi_fine_lista_strumentata(*a, **kw):
        t0 = time.time()
        r = await decidi_fine_lista_reale(*a, **kw)
        diario["tempi"]["scroll"] += time.time() - t0
        return r

    _sostituisci("decidi_fine_lista", decidi_fine_lista_strumentata)

    lancia_reale = motore.lancia

    async def lancia_strumentata(*a, **kw):
        t0 = time.time()
        r = await lancia_reale(*a, **kw)
        diario["tempi"]["scroll"] += time.time() - t0
        return r

    _sostituisci("lancia", lancia_strumentata)

    def ripristina():
        for nome_attributo, funzione in originali.items():
            setattr(motore, nome_attributo, funzione)

    return ripristina


async def raccolta(db, campaign_id, segnalibro):
    """Chiama il motore vero (`run_inbox_browser_list`), avvolto per misurare.

    Il budget di sessione (30-55 min) e' interno al motore, casuale ad ogni
    chiamata: qui c'e' solo una rete di sicurezza esterna (TIMEOUT_SICUREZZA_S),
    non un timer che guida la raccolta.
    """
    log(f"FASE B — raccolta supervisionata (motore vero, segnalibro={'ON' if segnalibro else 'OFF'})")

    campaign = (await db.execute(
        select(Campaign).where(Campaign.id == campaign_id)
    )).scalar_one()
    if campaign.inbox_engine != "browser":
        log(f"  !! campaign.inbox_engine='{campaign.inbox_engine}', non 'browser' — il motore uscira' subito")

    # Stessa strada di scrape_list.py: attributo RUNTIME, mai persistito.
    campaign.inbox_salta_lavorate = segnalibro
    # _deve_fermarsi() dentro il motore si ferma all'istante se lo stato non
    # e' listing/listing_break: va impostato e committato PRIMA di chiamare.
    campaign.status = CampaignStatus.listing
    campaign.updated_at = datetime.utcnow()
    await db.commit()

    diario = {
        "aperture": [],
        "fallimenti_header": [],
        "tempi": {"lettura": 0.0, "apertura": 0.0, "scroll": 0.0},
        "creati": 0, "aggiornati": 0,
        "righe_saltate": 0, "righe_ripetute": 0,
    }

    ripristina = _instrumenta(diario)
    try:
        esito = await asyncio.wait_for(
            motore.run_inbox_browser_list(campaign_id, db, campaign),
            timeout=TIMEOUT_SICUREZZA_S,
        )
        diario["esito_run"] = esito  # secondi di defer al break, o None
    except asyncio.TimeoutError:
        diario["esito_run"] = "timeout_sicurezza"
        log(f"  !! TIMEOUT DI SICUREZZA a {TIMEOUT_SICUREZZA_S}s — il motore non e' rientrato da solo")
    finally:
        ripristina()

    log(f"FASE B finita: {diario['creati']} creati, {diario['aggiornati']} aggiornati, "
        f"{diario['righe_saltate']} righe saltate (segnalibro), "
        f"{diario['righe_ripetute']} righe ripetute")
    return diario


async def main():
    # sys.argv[1]: 1/0 = modalita' segnalibro ON/OFF per la FASE B (Task 12:
    # prima una misura con OFF, poi una con ON, per confrontare il guadagno).
    segnalibro = bool(int(sys.argv[1])) if len(sys.argv) > 1 else False

    async with AsyncSessionLocal() as db:
        acct = (await db.execute(select(InstagramAccount).where(
            InstagramAccount.username == ACCOUNT))).scalar_one_or_none()
        if acct is None:
            log(f"account {ACCOUNT} non trovato")
            return

        rapporto = {"inizio": datetime.now().isoformat(timespec="seconds"), "segnalibro": segnalibro}

        # FASE A: sessione propria, chiusa PRIMA della fase B — run_inbox_browser_list
        # apre la sua BrowserSession sullo stesso profilo, e le due non possono
        # convivere (lock profilo browser cross-processo, gia' visto in questo
        # cantiere: due sessioni aperte insieme sullo stesso account si bloccano
        # a vicenda).
        if os.environ.get("CENSIMENTO", "1") == "1":
            session = BrowserSession(acct.id)
            await session.open()
            try:
                ctx = session.context
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://www.instagram.com/direct/inbox/",
                                wait_until="commit", timeout=60000)
                await page.wait_for_timeout(8000)
                rapporto["viewport"] = await page.evaluate(
                    "() => ({w: window.innerWidth, h: window.innerHeight})")
                log(f"viewport reale: {rapporto['viewport']}")

                viste, ordine, formati = await censimento(page)
                rapporto["censite"] = {k: v for k, v in viste.items()}
                rapporto["ordine_censimento"] = ordine
                rapporto["formati_data_riga"] = formati
            finally:
                await session.close()

        try:
            rapporto["diario"] = await raccolta(db, CAMPAGNA, segnalibro)
        finally:
            rapporto["fine"] = datetime.now().isoformat(timespec="seconds")
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(rapporto, f, ensure_ascii=False, indent=2, default=str)
            log(f"rapporto salvato in {OUT}")


asyncio.run(main())
