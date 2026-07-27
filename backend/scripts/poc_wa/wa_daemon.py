# backend/scripts/poc_wa/wa_daemon.py
"""Sessione WhatsApp PERSISTENTE: si apre una volta e resta viva.

PERCHE' ESISTE (osservazione di Tommaso, 27/07). Ogni script del PoC apriva e
chiudeva il browser: 10 cicli apri/chiudi in 45 minuti solo di diagnostica. A
regime sarebbe stato strutturale — heartbeat quotidiano, scan ogni 15 minuti,
invii, ognuno un ciclo completo — decine al giorno per 14 giorni. Nessun umano
usa WhatsApp Web cosi': lo si apre la mattina e lo si lascia aperto. Ogni
riapertura rifa' anche l'handshake col telefono e risincronizza, quindi non e'
gratis nemmeno lato WhatsApp.

E' anche la forma che servira' in M1: il watcher e' un processo lungo, non una
sequenza di script.

PERCHE' NON via debug remoto. L'alternativa comoda era lasciare il browser
aperto e far connettere gli script dall'esterno (CDP). Scartata: Patchright
applica le sue protezioni anti-rilevamento AL LANCIO, e una connessione
esterna rischia di aggirarle proprio mentre si cerca di non dare nell'occhio.
Non verificabile qui, quindi non si scambia un rischio noto con uno ignoto.

COME SI PILOTA. Il daemon guarda `D:\\dev\\wa-poc\\comandi\\*.json`. Si lascia
un file, lui lo esegue nella sessione gia' aperta e scrive l'esito in
`comandi/esiti/`. Nessuna porta aperta, nessun canale in piu'.

    echo {"tipo":"scan"} > D:\\dev\\wa-poc\\comandi\\001.json

Comandi: scan · heartbeat · apri_chat (numero, sola lettura) · stop
L'INVIO NON E' QUI: resta in poc2_send, che ha le sue quattro guardie. Il
daemon non deve poter mandare messaggi per il solo fatto di essere in ascolto.

Uso:  python wa_daemon.py [--scan-ogni 15] [--heartbeat-ogni 120]
"""
import argparse
import asyncio
import json
import random
import traceback
from datetime import datetime, timezone
from pathlib import Path

from _common import POC_ROOT, artifacts_dir, log_event, snap, wa_context
from poc3_scan import PANE_SEL, scan_once
from wa_lib import AllowList, mask_pii, normalize_e164

COMANDI_DIR = Path(POC_ROOT) / "comandi"
ESITI_DIR = COMANDI_DIR / "esiti"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stampa(*a) -> None:
    """print con flush. Python bufferizza stdout quando non e' un terminale:
    un processo che gira per ore non mostrerebbe NULLA finche' il buffer non si
    riempie, e il log a schermo di un daemon serve proprio a vederlo vivo."""
    print(*a, flush=True)


def _jitter(minuti: float) -> float:
    """Intervallo con +/-25% di rumore. Un'azione esattamente ogni 900.000 ms
    e' una firma; il PoC non guadagna nulla dalla puntualita' al secondo."""
    base = minuti * 60
    return base * random.uniform(0.75, 1.25)


async def _esegui(page, cmd: dict) -> dict:
    tipo = cmd.get("tipo")

    if tipo == "scan":
        righe = await scan_once(page)
        path = artifacts_dir() / f"scan_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(righe, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "righe": len(righe),
                "non_letti": sum(1 for r in righe if r["unread_count"] > 0),
                "file": str(path)}

    if tipo == "heartbeat":
        # "Sono ancora loggato?" senza ricaricare: un reload e' proprio il
        # genere di azione che si vuole evitare di ripetere a comando.
        vivo = await page.locator(PANE_SEL).count() > 0
        righe = await page.locator("[role='row']").count()
        return {"ok": vivo, "sessione_viva": vivo, "righe_nel_dom": righe}

    if tipo == "apri_chat":
        e164 = normalize_e164(cmd.get("numero", ""))
        AllowList.load().assert_allowed(e164)      # fail-closed anche qui
        return {"ok": False, "errore": "apri_chat non ancora implementato nel daemon",
                "numero_masked": mask_pii(e164)}

    return {"ok": False, "errore": f"tipo sconosciuto: {tipo!r}"}


async def main(scan_ogni: float, heartbeat_ogni: float) -> None:
    COMANDI_DIR.mkdir(parents=True, exist_ok=True)
    ESITI_DIR.mkdir(parents=True, exist_ok=True)

    async with wa_context(headless=False) as (context, page):
        try:
            await page.wait_for_selector(PANE_SEL, timeout=90000)
        except Exception as e:
            await snap(page, "daemon-pane-mai-comparso")
            raise SystemExit(f"{PANE_SEL} non comparso in 90s: sessione da ricontrollare.") from e

        log_event("daemon_avviato", scan_ogni_min=scan_ogni, heartbeat_ogni_min=heartbeat_ogni)
        stampa(f"[{_ts()}] Sessione aperta. Comandi in: {COMANDI_DIR}")
        stampa("  Lascia un file JSON li' dentro, es. {\"tipo\":\"scan\"}. "
              "Per fermare: {\"tipo\":\"stop\"} oppure Ctrl+C.")

        prossimo_scan = asyncio.get_event_loop().time() + _jitter(scan_ogni)
        prossimo_hb = asyncio.get_event_loop().time() + _jitter(heartbeat_ogni)

        while True:
            adesso = asyncio.get_event_loop().time()

            # --- comandi lasciati su disco --------------------------------
            for f in sorted(COMANDI_DIR.glob("*.json")):
                try:
                    cmd = json.loads(f.read_text(encoding="utf-8-sig"))
                except Exception as exc:
                    esito = {"ok": False, "errore": f"JSON illeggibile: {exc}"}
                    cmd = {}
                else:
                    if cmd.get("tipo") == "stop":
                        f.unlink(missing_ok=True)
                        log_event("daemon_stop_richiesto")
                        stampa(f"[{_ts()}] stop richiesto: chiudo la sessione.")
                        return
                    try:
                        esito = await _esegui(page, cmd)
                    except Exception as exc:
                        # Un comando che esplode non deve portarsi via la
                        # sessione: e' il bene piu' costoso da ricreare.
                        esito = {"ok": False, "errore": f"{type(exc).__name__}: {exc}"[:300],
                                 "traceback": traceback.format_exc()[-800:]}

                esito["comando"] = cmd
                esito["ts"] = _ts()
                (ESITI_DIR / f.name).write_text(
                    json.dumps(esito, ensure_ascii=False, indent=2), encoding="utf-8")
                f.unlink(missing_ok=True)
                log_event("daemon_comando", tipo=cmd.get("tipo"), ok=esito.get("ok"))
                stampa(f"[{_ts()}] {cmd.get('tipo')} -> ok={esito.get('ok')} "
                      f"{ {k: v for k, v in esito.items() if k in ('righe', 'non_letti', 'errore')} }")

            # --- lavoro periodico -----------------------------------------
            if adesso >= prossimo_scan:
                try:
                    esito = await _esegui(page, {"tipo": "scan"})
                    stampa(f"[{_ts()}] scan periodico -> {esito.get('righe')} righe, "
                          f"{esito.get('non_letti')} non letti")
                except Exception as exc:
                    log_event("daemon_scan_fallito", errore=f"{type(exc).__name__}: {exc}"[:200])
                prossimo_scan = adesso + _jitter(scan_ogni)

            if adesso >= prossimo_hb:
                try:
                    esito = await _esegui(page, {"tipo": "heartbeat"})
                    log_event("daemon_heartbeat", **{k: v for k, v in esito.items() if k != "comando"})
                    stampa(f"[{_ts()}] heartbeat -> sessione_viva={esito.get('sessione_viva')}")
                    if not esito.get("sessione_viva"):
                        await snap(page, "daemon-sessione-persa")
                        stampa("  ATTENZIONE: la lista chat non risponde piu'. "
                              "Potrebbe essere un logout: controlla lo screenshot.")
                except Exception as exc:
                    log_event("daemon_heartbeat_fallito", errore=f"{type(exc).__name__}: {exc}"[:200])
                prossimo_hb = adesso + _jitter(heartbeat_ogni)

            await asyncio.sleep(5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-ogni", type=float, default=15, help="minuti tra gli scan (default 15)")
    ap.add_argument("--heartbeat-ogni", type=float, default=120, help="minuti tra gli heartbeat")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.scan_ogni, args.heartbeat_ogni))
    except KeyboardInterrupt:
        stampa("\nInterrotto: sessione chiusa.")
