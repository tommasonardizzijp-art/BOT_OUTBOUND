"""PoC-5 (11/08): le etichette di WhatsApp Business esistono su Web?

E' la prima decisione aperta della Sessione B, e blocca il disegno: l'intero
§5.3 dello spec auto-discover ("una lista per volta") e' costruito sull'ipotesi
che l'operatore possa scegliere una Lista/etichetta di WhatsApp e farla
scansionare. Il PoC-4 ha trovato una barra `div[role='tablist']` e l'ha
registrata come "liste trovate", ma dentro c'era "Tutte / Da leggere /
Preferiti": sono i filtri STANDARD di WhatsApp Web, non le etichette custom di
WhatsApp Business. Sono due cose diverse e la differenza decide il design.

SOLA LETTURA: nessun invio, nessun testo digitato, nessuna chat aperta. Si
clicca al massimo un filtro/menu per vederne il contenuto, che non modifica
niente. Gira NON headless apposta: Tommaso guarda e puo' fermare.

Metodo ereditato dal motore inbox browser (modulo inbox_browser/pagina.py):
IL JS RACCOGLIE, PYTHON DECIDE. Nessun selettore di etichetta viene inventato
qui dentro -- si fa il dump di tutto cio' che nella colonna sinistra ha un
aria-label, un data-icon o un title, con la sua posizione, e le parole chiave
si cercano in Python sul raccolto. Un selettore cablato che non aggancia dà
"non trovato" identico a "non esiste", ed e' esattamente l'errore che nel
motore Instagram e' costato un modulo che non salvava niente.

Uso (dal folder backend, MAI da un worktree -- i path dei profili sono relativi):
    ./venv/Scripts/python.exe scripts/poc_wa/poc5_etichette.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Il PoC-4 si lanciava con PYTHONPATH gia' impostato; qui si rende esplicito,
# come in scripts/registra_scroll_umano.py: uno script che muore su
# "No module named 'app'" fa perdere il giro di browser, non solo il comando.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import AsyncSessionLocal
from app.browser.whatsapp_page import WhatsAppWebPage
from app.services.wa_session import WHATSAPP_WEB_URL, _open_wa_browser, _wa_number_or_raise

# Il numero si passa da riga di comando: il primo giro (11/08) ha misurato
# PRIMERO TEST e ha scoperto che e' WhatsApp NORMALE, quindi le etichette
# Business li' non esisteranno mai. Tommaso ha WhatsApp Business sul numero
# personale, e i clienti veri "quasi tutti" lo avranno: la domanda "le
# etichette sono pilotabili da Web?" si risponde solo su un account che le ha.
NUMERI_NOTI = {
    "primero": "8c578c08-6659-43fa-9840-f55e88e220fc",   # WhatsApp normale
    "personale": "cd844927-3eec-43f1-8b86-30b8811c5cb9",  # WhatsApp Business
}
NUMERO = NUMERI_NOTI.get(sys.argv[1] if len(sys.argv) > 1 else "primero",
                         sys.argv[1] if len(sys.argv) > 1 else NUMERI_NOTI["primero"])
ETICHETTA_GIRO = sys.argv[1] if len(sys.argv) > 1 else "primero"
# `... personale sorveglia` -> resta aperto e campiona sync%/liste ogni 90s.
SORVEGLIA = "sorveglia" in sys.argv[2:]
OUT_DIR = Path(__file__).parent / "artifacts"

# Parole che, nel testo raccolto, indicano etichette VERE di WhatsApp Business
# (non i filtri standard). Si cercano in italiano e inglese: il censimento
# dell'inbox Instagram ha trovato interfaccia in inglese su un account trattato
# come italiano, quindi non si assume la lingua.
PAROLE_ETICHETTA = ("etichett", "label")
# I filtri standard: se il tablist contiene SOLO questi, non sono etichette.
PAROLE_FILTRO_STANDARD = ("tutte", "all", "da leggere", "unread", "preferit",
                          "favourite", "favorite", "gruppi", "groups")
# Le "Liste" sono una cosa TERZA: filtri custom che esistono anche su WhatsApp
# normale (su Primero ci sono, ma vuote perche' mai usate). Non confonderle con
# le etichette Business. 'Lista delle chat' e' invece l'aria-label del
# contenitore della sidebar: va escluso o produce un falso positivo su ogni
# account del mondo.
PAROLE_LISTA = ("liste", "lists")
ESCLUSI_LISTA = ("lista delle chat", "chat list")
# Marcatori che distinguono un account Business da uno normale.
PAROLE_BUSINESS = ("strumenti per le aziende", "business tools", "catalogo",
                   "catalog", "account aziendale", "business account")

# Raccoglie ogni elemento potenzialmente cliccabile con un'etichetta testuale,
# con posizione e attributi. Nessun filtro per coordinate qui dentro: la
# colonna la decide Python, sotto, col rettangolo vero di #pane-side.
_JS_CENSIMENTO = """() => {
  const nodi = [...document.querySelectorAll(
    '[aria-label], [data-icon], [title], [role="tab"], [role="button"], button')];
  const viewport = {w: window.innerWidth, h: window.innerHeight};
  const pane = document.querySelector('#pane-side');
  const paneRect = pane ? pane.getBoundingClientRect() : null;
  const grid = document.querySelector('[role="grid"]') ||
               document.querySelector('[aria-rowcount]');
  return {
    viewport,
    pane: paneRect ? {left: Math.round(paneRect.left), right: Math.round(paneRect.right),
                      top: Math.round(paneRect.top), w: Math.round(paneRect.width)} : null,
    rowcount: grid ? grid.getAttribute('aria-rowcount') : null,
    nodi: nodi.map(e => {
      const r = e.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return null;
      return {
        tag: e.tagName.toLowerCase(),
        ruolo: e.getAttribute('role'),
        aria: e.getAttribute('aria-label'),
        icona: e.getAttribute('data-icon'),
        title: e.getAttribute('title'),
        testo: (e.innerText || '').trim().slice(0, 80),
        left: Math.round(r.left), right: Math.round(r.right),
        top: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
      };
    }).filter(Boolean),
  };
}"""


def nella_colonna_sinistra(nodo: dict, pane: dict | None, larghezza: int) -> bool:
    """Il nodo appartiene alla colonna della lista chat (o alla barra laterale).

    Il confine si MISURA da #pane-side, non si cabla: e' la stessa regola per
    cui il motore inbox calcola `bordo_colonne` a runtime. Senza #pane-side si
    ripiega su una frazione della finestra, dichiarandolo nel referto.
    """
    limite = pane["right"] if pane else larghezza * 0.4
    return float(nodo.get("left", 0)) < limite


def cerca_parole(testi: list[str], parole: tuple[str, ...]) -> list[str]:
    trovati = []
    for t in testi:
        basso = (t or "").lower()
        if any(p in basso for p in parole):
            trovati.append(t)
    return trovati


def voci_tablist(testo_tablist: str | None) -> list[str]:
    """Le voci vere della barra filtri, senza i badge.

    Il primo giro su Primero ha letto 'Tutte / Da leggere / 11 / Preferiti':
    quell'11 e' il contatore dei non letti dentro 'Da leggere', non una voce.
    Contandolo come voce sconosciuta il verdetto usciva 'incerto' su un dato
    in realta' limpido — un difetto della funzione, non della misura.
    """
    voci = [v.strip() for v in (testo_tablist or "").split("\n") if v.strip()]
    return [v for v in voci if not v.replace(".", "").replace(",", "").isdigit()]


def verdetto_etichette(candidati_etichetta: list[str], testo_tablist: str | None) -> str:
    """'etichette_custom' | 'solo_filtri_standard' | 'incerto'.

    Funzione pura: la decisione che cambia il design non deve stare dentro una
    query JS ne' dipendere dal browser, cosi' e' verificabile con i dati veri
    del referto anche fra un mese.
    """
    if candidati_etichetta:
        return "etichette_custom"
    voci = voci_tablist(testo_tablist)
    if voci and all(cerca_parole([v], PAROLE_FILTRO_STANDARD) for v in voci):
        return "solo_filtri_standard"
    return "incerto"


def candidati_lista(testi: list[str]) -> list[str]:
    """Le 'Liste' vere, senza l'aria-label del contenitore della sidebar."""
    grezzi = cerca_parole(testi, PAROLE_LISTA)
    return sorted({t for t in grezzi
                   if not any(e in (t or "").lower() for e in ESCLUSI_LISTA)})


def salva(referto: dict) -> Path:
    out = OUT_DIR / f"poc5_etichette_{ETICHETTA_GIRO}.json"
    out.write_text(json.dumps(referto, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


async def scatta(page, tag: str) -> str | None:
    try:
        path = OUT_DIR / f"poc5_{tag}_{int(time.time())}.png"
        await page.screenshot(path=str(path))
        return str(path)
    except Exception:
        return None


async def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionLocal() as db:
        numero = await _wa_number_or_raise(db, NUMERO)
        proxy_url = numero.proxy_url
        etichetta_numero = numero.label

    referto: dict = {"numero": etichetta_numero, "verdetto": None}

    # headless=False per scelta di Tommaso: vuole vedere e poter fermare.
    async with _open_wa_browser(NUMERO, headless=False, proxy_url=proxy_url) as context:
        page = await context.new_page()
        # 30s (il default) non bastano col browser in finestra: il primo giro e'
        # morto esattamente qui. Si aspetta il "commit" della navigazione, non il
        # domcontentloaded — WhatsApp Web continua a caricare risorse ben oltre —
        # e poi la comparsa della UI vera, con pazienza crescente come fa
        # decidi_fine_lista nel motore inbox.
        await page.goto(WHATSAPP_WEB_URL, wait_until="commit", timeout=120_000)
        for attesa_s in (3, 5, 10, 20, 30):
            await page.wait_for_timeout(attesa_s * 1000)
            pronto = await page.evaluate(
                "() => !!(document.querySelector('#pane-side') "
                "|| document.querySelector('canvas') "
                "|| document.querySelector('[data-icon=\"intro-md-beta-logo-dark\"]'))"
            )
            print(f"  ...attesa UI {attesa_s}s -> pronto={pronto}", file=sys.stderr)
            if pronto:
                break
        pom = WhatsAppWebPage(page)
        stato = await pom.session_state()
        print(f"session_state={stato}", file=sys.stderr)
        referto["session_state"] = stato
        if stato != "logged_in":
            referto["screenshot"] = await scatta(page, "non_loggato")
            print(json.dumps(referto, ensure_ascii=False, indent=2))
            return

        await page.wait_for_timeout(2500)   # la sidebar finisce di popolarsi
        censimento = await page.evaluate(_JS_CENSIMENTO)
        pane = censimento.get("pane")
        larghezza = (censimento.get("viewport") or {}).get("w", 0)
        referto["pane_misurato"] = pane
        referto["aria_rowcount"] = censimento.get("rowcount")

        sinistra = [n for n in censimento["nodi"]
                    if nella_colonna_sinistra(n, pane, larghezza)]
        referto["nodi_colonna_sinistra"] = len(sinistra)

        # Tutto il testo utile di quei nodi, in un solo posto: e' su questo che
        # si cerca, non su un selettore indovinato.
        testi = []
        for n in sinistra:
            for campo in ("aria", "title", "icona", "testo"):
                if n.get(campo):
                    testi.append(n[campo])
        referto["candidati_etichetta"] = sorted(set(cerca_parole(testi, PAROLE_ETICHETTA)))

        # Il tablist del PoC-4, riletto per confronto diretto.
        tablist = [n for n in sinistra if n.get("ruolo") == "tablist"
                   or (n.get("ruolo") == "tab")]
        testo_tablist = None
        try:
            loc = page.locator("div[role='tablist']").first
            if await loc.count():
                testo_tablist = (await loc.inner_text())[:300]
        except Exception:
            pass
        referto["tablist_testo"] = testo_tablist
        referto["tablist_nodi"] = len(tablist)

        referto["verdetto"] = verdetto_etichette(referto["candidati_etichetta"], testo_tablist)

        # Le icone della barra laterale, per intero: se le etichette esistono
        # ma con un nome che non abbiamo previsto, si vedono qui invece di
        # sparire in un "non trovato".
        referto["icone_sidebar"] = sorted({n["icona"] for n in sinistra if n.get("icona")})
        referto["aria_sidebar"] = sorted({n["aria"] for n in sinistra if n.get("aria")})[:60]
        referto["candidati_lista"] = candidati_lista(testi)
        referto["marcatori_business"] = sorted(set(cerca_parole(testi, PAROLE_BUSINESS)))

        referto["screenshot_sidebar"] = await scatta(page, f"{ETICHETTA_GIRO}_sidebar")

        # Secondo censimento col MENU APERTO. Il primo giro ha guardato solo
        # cio' che era gia' visibile e ha concluso "nessuna etichetta": vero su
        # quell'account, ma un menu chiuso e' un buco nella misura, non
        # un'assenza. Aprire un menu non modifica niente — resta sola lettura.
        # Il primo giro sul Business ha censito TUTTA la pagina col menu
        # aperto, quindi ha raccolto i nomi delle chat e non si e' potuto dire
        # cosa ci fosse davvero dentro il menu: una misura debole travestita da
        # risposta. Qui si censisce solo cio' che il click ha FATTO COMPARIRE —
        # la differenza fra il DOM prima e dopo — e si prova ogni voce
        # candidata, non solo 'Menu'. 'Strumenti' e 'Pubblicizza' sono le voci
        # Business, ed e' li' che le etichette vivrebbero.
        referto["pannelli"] = {}
        for voce in ("Strumenti", "Menu", "Pubblicizza", "Impostazioni"):
            prima = {t for t in testi}
            esito: dict = {"cliccato": False}
            try:
                loc = page.locator(f"[aria-label='{voce}']").first
                if not await loc.count():
                    esito["errore"] = "voce assente"
                    referto["pannelli"][voce] = esito
                    continue
                await loc.click(timeout=4000)
                await page.wait_for_timeout(1500)
                cens = await page.evaluate(_JS_CENSIMENTO)
                testi_ora = []
                for n in cens["nodi"]:
                    for campo in ("aria", "title", "icona", "testo"):
                        if n.get(campo):
                            testi_ora.append(n[campo])
                # Solo il NUOVO: cio' che non c'era prima del click.
                nuovi = sorted({t for t in testi_ora if t and t not in prima and len(t) < 60})
                esito = {
                    "cliccato": True,
                    "voci_nuove": nuovi[:60],
                    "candidati_etichetta": sorted(set(cerca_parole(nuovi, PAROLE_ETICHETTA))),
                    "candidati_lista": candidati_lista(nuovi),
                    "marcatori_business": sorted(set(cerca_parole(nuovi, PAROLE_BUSINESS))),
                    "screenshot": await scatta(page, f"{ETICHETTA_GIRO}_{voce.lower()}"),
                }
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(600)
            except Exception as exc:
                esito["errore"] = f"{type(exc).__name__}: {exc}"
            referto["pannelli"][voce] = esito

        # PERCENTUALE DI SINCRONIZZAZIONE (osservata da Tommaso l'11/08 dentro
        # Impostazioni, icona 'ic-sync'). Serve come GATE della Fase A, non solo
        # come informazione: scansionare la sidebar mentre WhatsApp sta ancora
        # tirando giu' la cronologia significa raccogliere una frazione delle
        # chat e dichiararla completa — un "esaurito" falso, che e' il modo in
        # cui questo canale ha gia' prodotto una campagna 'completed' con zero
        # invii. Qui si cerca DOVE sta il numero: se non e' leggibile, il piano
        # non deve prometterlo.
        referto["sincronizzazione"] = {"trovato": False}
        try:
            imp = page.locator("[aria-label='Impostazioni']").first
            if await imp.count():
                await imp.click(timeout=4000)
                await page.wait_for_timeout(2000)
                sync = await page.evaluate("""() => {
                  const testi = [...document.querySelectorAll('span, div, h1, h2, p')]
                    .filter(e => e.children.length === 0)
                    .map(e => (e.textContent || '').trim())
                    .filter(t => t && t.length < 120);
                  // Qualunque cosa contenga una percentuale o parli di sincronia.
                  const perc = testi.filter(t => /\\d+\\s*%/.test(t));
                  const sincro = testi.filter(t => /sincroniz|syncing|sync/i.test(t));
                  const icone = [...document.querySelectorAll('[data-icon]')]
                    .map(e => e.getAttribute('data-icon'))
                    .filter(i => /sync/i.test(i));
                  return {percentuali: [...new Set(perc)].slice(0, 20),
                          righe_sincro: [...new Set(sincro)].slice(0, 20),
                          icone_sync: [...new Set(icone)]};
                }""")
                referto["sincronizzazione"] = {
                    "trovato": bool(sync["percentuali"] or sync["righe_sincro"]),
                    **sync,
                    "screenshot": await scatta(page, f"{ETICHETTA_GIRO}_sync"),
                }
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
        except Exception as exc:
            referto["sincronizzazione"] = {"trovato": False,
                                           "errore": f"{type(exc).__name__}: {exc}"}

        # Il verdetto tiene conto anche di cio' che era dietro i pannelli.
        tutti_etichetta = list(referto["candidati_etichetta"])
        for esito in referto["pannelli"].values():
            tutti_etichetta += esito.get("candidati_etichetta") or []
        referto["verdetto"] = verdetto_etichette(sorted(set(tutti_etichetta)), testo_tablist)

        # Quante chat hanno GIA' il numero come titolo: sono quelle per cui la
        # Fase A non deve aprire nulla. Il costo misurato dal PoC-4 (5,3s/chat)
        # vale per l'apertura del pannello info; ogni riga che si risolve dalla
        # sola sidebar esce da quel conto.
        righe = await pom.scan_chat_list()
        con_numero = sum(1 for r in righe if r.title_is_number)
        referto["righe_dom"] = len(righe)
        referto["righe_titolo_e_numero"] = con_numero
        referto["esempi_titolo"] = [
            {"title_is_number": r.title_is_number,
             "title": r.title if not r.title_is_number else "<numero>"}
            for r in righe[:15]
        ]

        # Il referto si salva QUI, non a fine funzione: il primo giro buono e'
        # morto stampando (console cp1252 contro i nomi veri delle chat) e ha
        # buttato via una misura gia' fatta col browser gia' aperto.
        salva(referto)
        print(json.dumps(referto, ensure_ascii=True, indent=2))

        # MODALITA' SORVEGLIANZA (`... personale sorveglia`). Le Liste sono
        # risultate VUOTE su Web mentre sul telefono sono piene: l'ipotesi di
        # Tommaso e' che sia effetto della sincronizzazione ancora in corso, non
        # un limite strutturale di WhatsApp Web. Non e' decidibile con una
        # fotografia — serve una SERIE: si ricontrolla ogni 90s la percentuale
        # di sync e la presenza di liste, e si guarda se le due cose si muovono
        # insieme. Se le liste compaiono avvicinandosi al 100%, il filtro per
        # lista torna praticabile e il design puo' riprenderlo.
        if SORVEGLIA:
            referto["sorveglianza"] = []
            for giro in range(60):          # 60 x 90s = 90 minuti al massimo
                if page.is_closed():
                    break
                await page.wait_for_timeout(90_000)
                if page.is_closed():
                    break
                try:
                    campione = {"giro": giro}
                    imp = page.locator("[aria-label='Impostazioni']").first
                    if await imp.count():
                        await imp.click(timeout=4000)
                        await page.wait_for_timeout(1800)
                        campione["sync"] = await page.evaluate("""() => {
                          const testi = [...document.querySelectorAll('span, div, p')]
                            .filter(e => e.children.length === 0)
                            .map(e => (e.textContent || '').trim());
                          return [...new Set(testi.filter(t => /\\d+\\s*%/.test(t)
                                 || /sincroniz|syncing/i.test(t)))].slice(0, 10);
                        }""")
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(600)
                    strumenti = page.locator("[aria-label='Strumenti']").first
                    if await strumenti.count():
                        await strumenti.click(timeout=4000)
                        await page.wait_for_timeout(1800)
                        cens = await page.evaluate(_JS_CENSIMENTO)
                        testi_ora = [n[c] for n in cens["nodi"]
                                     for c in ("aria", "title", "testo") if n.get(c)]
                        campione["liste"] = candidati_lista(testi_ora)
                        campione["etichette"] = sorted(set(
                            cerca_parole(testi_ora, PAROLE_ETICHETTA)))
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(600)
                    referto["sorveglianza"].append(campione)
                    salva(referto)     # ogni giro: se il PC si spegne, resta il misurato
                    print(f"  [sorveglianza {giro}] sync={campione.get('sync')} "
                          f"liste={campione.get('liste')}", file=sys.stderr)
                except Exception as exc:
                    referto["sorveglianza"].append({"giro": giro,
                                                    "errore": f"{type(exc).__name__}: {exc}"})
                    salva(referto)

        print("\n>>> La finestra resta aperta: guarda la sidebar e cerca le etichette "
              "a mano (menu in alto, icone laterali). CHIUDI la finestra quando hai finito.",
              file=sys.stderr)

        try:
            while not page.is_closed():
                await page.wait_for_timeout(1000)
        except Exception:
            pass

    print(f"\n[OK] referto in {salva(referto)}", file=sys.stderr)


# La console di Windows e' cp1252: senza questo, un nome di chat con un
# carattere fuori tabella uccide lo script DOPO che la misura e' riuscita.
for flusso in (sys.stdout, sys.stderr):
    try:
        flusso.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

asyncio.run(main())
