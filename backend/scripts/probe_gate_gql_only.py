"""Probe LIVE del gate professional in condizioni POST-INVERSIONE (gql-only).

Perche' esiste: i probe precedenti valutavano il gate preferendo la sorgente web
(passiva o fetch esplicito). Dopo l'inversione la fetch esplicita non parte quasi
mai, quindi in produzione il gate leggera' quasi sempre SOLO il GraphQL passivo.
Questo probe riproduce quella condizione: NON fa mai la fetch esplicita di
web_profile_info, ne' chiama /info/.

Domanda a cui risponde, ed e' quella che decide il design del gate:

    esiste un profilo che ha reso un contatto da /info/ e che il GraphQL
    da solo NON marca professional?

  - gql dice True  -> gate salvo
  - gql dice None  -> nessun segnale: l'escape "chiama comunque" lo salva,
                      ma la resa peggiora
  - gql dice False -> CONTATTO PERSO IN SILENZIO: il gate va cambiato

Verita' di riferimento: `contact_source` in DB ('ig_business' = risposta di
/info/, 'bio_regex' = pescato dal testo della bio).

Uso (dalla cartella backend):
    ./venv/Scripts/python.exe -m scripts.probe_gate_gql_only <account_id> [con_contatto] [senza]

Nessuna scrittura su DB. Zero richieste attribuibili al bot oltre alla visita.
"""
import asyncio
import json
import sys

from sqlalchemy import text

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.services.browser_bio import graphql_user_to_web_shape, web_user_to_shim

WEB_PROFILE_PATH = "/api/v1/users/web_profile_info/"
GRAPHQL_PATH = "/api/graphql"

# Esclude i profili gia' visti dai probe precedenti: campione fresco.
GIA_VISTI = (
    "flakodallapunta", "orticastello", "femminilifollie", "centrolucemilano",
    "calzature_pop", "la_vineria_aosta", "emilianomurzi", "_francesco_villani_",
    "elpigrollingdone", "cartolibreria.brivio", "desireeasiago", "hulahoopbabyshop",
    "giulaivi", "ilricciotralepagine", "elite_boutique_dal_2014",
    "hangarsettantanove", "daniele_alviani78", "gioielleriaquaranta",
)

TARGETS_SQL = """
(SELECT f.username, true AS da_info
 FROM followers f JOIN campaigns c ON c.id = f.campaign_id
 WHERE c.bio_engine = 'browser' AND f.biography IS NOT NULL
   AND f.contact_source LIKE '%%ig_business%%'
   AND f.username <> ALL(:visti)
 ORDER BY f.updated_at DESC LIMIT :n_con)
UNION ALL
(SELECT f.username, false
 FROM followers f JOIN campaigns c ON c.id = f.campaign_id
 WHERE c.bio_engine = 'browser' AND f.biography IS NOT NULL
   AND (f.contact_source IS NULL OR f.contact_source NOT LIKE '%%ig_business%%')
   AND f.username <> ALL(:visti)
 ORDER BY f.updated_at DESC LIMIT :n_senza)
"""

CAMPI_SHIM = ("pk", "username", "biography", "is_private", "is_verified",
              "follower_count", "following_count")


def professional_gql(u: dict | None):
    """La regola del gate applicata al SOLO payload GraphQL.
    Ritorna True / False / None (nessun segnale leggibile)."""
    if not u:
        return None
    if u.get("is_professional_account") is True:
        return True
    if u.get("account_type") in (2, 3):
        return True
    if u.get("is_business") is True:
        return True
    if (u.get("is_professional_account") is False
            or u.get("account_type") == 1
            or u.get("is_business") is False):
        return False
    return None


async def capture_gql_only(raw_page, username: str, attesa_s: float) -> dict:
    """Solo ascolto passivo. Nessuna fetch esplicita: e' il punto del probe."""
    got: dict = {}
    t_primo = {"gql": None}

    async def on_response(resp):
        try:
            if GRAPHQL_PATH in resp.url and resp.status == 200 and "gql" not in got:
                u = (((await resp.json()) or {}).get("data") or {}).get("user")
                if u and u.get("username") == username:
                    got["gql"] = u
                    t_primo["gql"] = asyncio.get_event_loop().time()
            elif WEB_PROFILE_PATH in resp.url and resp.status == 200 and "web" not in got:
                u = (((await resp.json()) or {}).get("data") or {}).get("user")
                if u:
                    got["web_passivo"] = True
        except Exception:
            pass

    raw_page.on("response", on_response)
    try:
        t0 = asyncio.get_event_loop().time()
        await raw_page.goto(f"https://www.instagram.com/{username}/",
                            wait_until="domcontentloaded", timeout=30000)
        atteso = 0.0
        while atteso < attesa_s and "gql" not in got:
            await asyncio.sleep(0.2)
            atteso += 0.2
        if t_primo["gql"]:
            got["t_gql"] = round(t_primo["gql"] - t0, 2)
        return got
    finally:
        try:
            raw_page.remove_listener("response", on_response)
        except Exception:
            pass


async def main(account_id: str, n_con: int = 25, n_senza: int = 10,
               attesa_s: float = 8.0) -> None:
    async with AsyncSessionLocal() as db:
        res = await db.execute(text(TARGETS_SQL),
                               {"visti": list(GIA_VISTI), "n_con": n_con, "n_senza": n_senza})
        targets = [(r[0], r[1]) for r in res.fetchall()]
    print(f"Campione fresco: {len(targets)} profili "
          f"({sum(1 for _, d in targets if d)} con contatto da /info/, "
          f"{sum(1 for _, d in targets if not d)} senza)")
    print(f"Modalita': SOLO ascolto passivo. Nessuna fetch esplicita, nessuna /info/.\n")

    session = BrowserSession(account_id, headless=False)
    await session.open()
    await session.page.ensure_logged_in(account_id, allow_login=False)
    raw_page = await session.page._get_page()

    righe, tempi, web_passivi, mancanti = [], [], 0, {}
    try:
        for i, (uname, da_info) in enumerate(targets):
            got = await capture_gql_only(raw_page, uname, attesa_s)

            url = raw_page.url
            if any(k in url for k in ("challenge", "warning", "checkpoint")):
                print(f"\n!! INTERSTIZIALE dopo {i} profili: {url[:110]}\n!! Mi fermo.")
                break

            gql = got.get("gql")
            p = professional_gql(gql)
            if got.get("web_passivo"):
                web_passivi += 1
            if got.get("t_gql") is not None:
                tempi.append(got["t_gql"])

            if gql:
                shim = web_user_to_shim(graphql_user_to_web_shape(gql))
                for c in CAMPI_SHIM:
                    if getattr(shim, c, None) in (None, ""):
                        mancanti[c] = mancanti.get(c, 0) + 1

            if da_info and p is False:
                esito = "PERSO"
            elif da_info and p is None:
                esito = "escape"
            elif da_info:
                esito = "ok   "
            else:
                esito = "     "
            print(f"[{i+1:>2}/{len(targets)}] {esito} @{uname:<28} "
                  f"info={'SI' if da_info else 'no':<3} gql_prof={p} "
                  f"t={got.get('t_gql', '-')}s")
            righe.append((uname, da_info, p))

            if i < len(targets) - 1:
                await asyncio.sleep(6.0)
    finally:
        await session.close()

    print("\n" + "=" * 72)
    if not righe:
        print("Nessun profilo letto.")
        return
    con = [r for r in righe if r[1]]
    persi = [r for r in con if r[2] is False]
    escape = [r for r in con if r[2] is None]
    print(f"VERDETTO su {len(con)} profili con contatto da /info/, letti col SOLO GraphQL:")
    print(f"  professional (gate salvo)      : {sum(1 for r in con if r[2] is True)}")
    print(f"  segnale assente (escape salva) : {len(escape)}"
          + (" -> " + ", ".join('@' + r[0] for r in escape) if escape else ""))
    print(f"  NEGATIVO = CONTATTO PERSO      : {len(persi)}"
          + (" -> " + ", ".join('@' + r[0] for r in persi) if persi else ""))

    letti = len(righe)
    prof = sum(1 for r in righe if r[2] is True)
    print(f"\nResa in condizioni gql-only: professional {prof}/{letti} ({prof*100//letti}%)"
          f" -> /info/ risparmiate {letti-prof}/{letti} ({(letti-prof)*100//letti}%)")
    print(f"web_profile_info colto passivamente: {web_passivi}/{letti}")
    if tempi:
        tempi.sort()
        print(f"arrivo del GraphQL: min {tempi[0]}s | mediana {tempi[len(tempi)//2]}s | "
              f"max {tempi[-1]}s  (su {len(tempi)} misure)")
    print(f"campi del Follower non coperti dal GraphQL: "
          f"{json.dumps(mancanti, ensure_ascii=False) if mancanti else 'nessuno'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python -m scripts.probe_gate_gql_only <account_id> [con] [senza]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1],
                     int(sys.argv[2]) if len(sys.argv) > 2 else 25,
                     int(sys.argv[3]) if len(sys.argv) > 3 else 10))
