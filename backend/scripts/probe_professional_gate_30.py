"""Probe LIVE a campione largo del gate 'professional' + della sorgente GraphQL.

Differenza dal probe a 7: NON chiama /info/. La verita' di riferimento e' gia' in
DB — `contact_source` distingue 'ig_business' (contatto arrivato da /info/) da
'bio_regex' (pescato dal testo della bio). Quindi il campione si allarga senza
rifare la chiamata che vogliamo evitare.

Risponde a tre domande in un solo passaggio:

  1. GATE — esiste un profilo che ha reso un contatto da /info/ ma NON risulta
     professional? Anche uno solo e il gate perde contatti e va scartato.
  2. RESA — che quota di profili e' professional? Dice quanto taglia il gate.
  3. SORGENTI — quante volte arriva il passivo web, quante il passivo GraphQL,
     quante web_profile_info muore di 400. E se il payload GraphQL da solo
     contiene tutti i campi che il codice scrive sul Follower (se si, si puo'
     invertire l'ordine e smettere di chiedere).

Uso (dalla cartella backend):
    ./venv/Scripts/python.exe -m scripts.probe_professional_gate_30 <account_id> [quanti]

Nessuna scrittura su DB.
"""
import asyncio
import json
import sys

from sqlalchemy import text

from app.browser.context_manager import BrowserSession
from app.database import AsyncSessionLocal
from app.services.browser_bio import WEB_APP_ID, graphql_user_to_web_shape, web_user_to_shim

WEB_PROFILE_PATH = "/api/v1/users/web_profile_info/"
GRAPHQL_PATH = "/api/graphql"

# Meta' campione con contatto da /info/ (il test critico), meta' senza.
# Solo campagne browser con bio davvero letta, altrimenti l'assenza di contatto
# non dimostra niente.
TARGETS_SQL = """
(SELECT f.username, true AS da_info
 FROM followers f JOIN campaigns c ON c.id = f.campaign_id
 WHERE c.bio_engine = 'browser' AND f.biography IS NOT NULL
   AND f.contact_source LIKE '%%ig_business%%'
 ORDER BY f.updated_at DESC LIMIT :meta)
UNION ALL
(SELECT f.username, false
 FROM followers f JOIN campaigns c ON c.id = f.campaign_id
 WHERE c.bio_engine = 'browser' AND f.biography IS NOT NULL
   AND (f.contact_source IS NULL OR f.contact_source NOT LIKE '%%ig_business%%')
 ORDER BY f.updated_at DESC LIMIT :meta)
"""

# Campi che il codice di produzione scrive sul Follower partendo dal payload.
# Se GraphQL li copre tutti, puo' fare da sorgente primaria.
CAMPI_SHIM = ("pk", "username", "full_name", "biography", "is_private", "is_verified",
              "follower_count", "following_count", "external_url", "bio_links")


def professional(u: dict | None) -> bool | None:
    """Regola candidata al gate, letta da ENTRAMBE le sorgenti: web espone
    is_professional_account, GraphQL espone account_type/is_business (e tiene
    is_professional_account sempre a null). None = nessun segnale leggibile."""
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


async def capture(raw_page, username: str, timeout_s: float = 6.0) -> dict:
    got: dict = {}

    async def on_response(resp):
        try:
            if WEB_PROFILE_PATH in resp.url and resp.status == 200 and "web" not in got:
                u = (((await resp.json()) or {}).get("data") or {}).get("user")
                if u:
                    got["web"], got["web_src"] = u, "passivo"
            elif GRAPHQL_PATH in resp.url and resp.status == 200 and "gql" not in got:
                u = (((await resp.json()) or {}).get("data") or {}).get("user")
                if u and u.get("username") == username:
                    got["gql"] = u
        except Exception:
            pass

    raw_page.on("response", on_response)
    try:
        await raw_page.goto(f"https://www.instagram.com/{username}/",
                            wait_until="domcontentloaded", timeout=30000)
        waited = 0.0
        while waited < timeout_s and "web" not in got:
            await asyncio.sleep(0.4)
            waited += 0.4
        if "web" not in got:
            res = await raw_page.evaluate(
                """async (args) => {
                    const [u, appId] = args;
                    const r = await fetch(
                        `/api/v1/users/web_profile_info/?username=${encodeURIComponent(u)}`,
                        { headers: { 'x-ig-app-id': appId }, credentials: 'include' });
                    if (!r.ok) return { __status: r.status };
                    return await r.json();
                }""",
                [username, WEB_APP_ID])
            if isinstance(res, dict) and res.get("__status"):
                got["web_http"] = res["__status"]
            else:
                u = (((res or {}).get("data") or {}).get("user"))
                if u:
                    got["web"], got["web_src"] = u, "fetch"
        return got
    finally:
        try:
            raw_page.remove_listener("response", on_response)
        except Exception:
            pass


def mancanti_gql(gql: dict | None) -> list[str]:
    """Campi che il codice scrive sul Follower e che il payload GraphQL NON copre."""
    if not gql:
        return ["(nessun payload)"]
    shim = web_user_to_shim(graphql_user_to_web_shape(gql))
    out = []
    for c in CAMPI_SHIM:
        v = getattr(shim, c, None)
        # external_url e bio_links legittimamente vuoti su molti profili: non
        # sono una lacuna della sorgente. Contano solo i campi sempre presenti.
        if v in (None, "") and c not in ("external_url", "bio_links", "full_name"):
            out.append(c)
    return out


async def main(account_id: str, quanti: int = 30) -> None:
    meta = max(1, quanti // 2)
    async with AsyncSessionLocal() as db:
        res = await db.execute(text(TARGETS_SQL), {"meta": meta})
        targets = [(r[0], r[1]) for r in res.fetchall()]
    print(f"Target: {len(targets)} ({sum(1 for _, d in targets if d)} con contatto da /info/, "
          f"{sum(1 for _, d in targets if not d)} senza)\n")

    session = BrowserSession(account_id, headless=False)
    await session.open()
    await session.page.ensure_logged_in(account_id, allow_login=False)
    raw_page = await session.page._get_page()

    righe, src_stat = [], {"web_passivo": 0, "web_fetch": 0, "web_400": 0,
                           "web_altro": 0, "gql_passivo": 0, "nessuna": 0}
    lacune: dict[str, int] = {}
    try:
        for i, (uname, da_info) in enumerate(targets):
            got = await capture(raw_page, uname)

            url = raw_page.url
            if any(k in url for k in ("challenge", "warning", "checkpoint")):
                print(f"\n!! INTERSTIZIALE dopo {i} profili: {url[:110]}\n!! Mi fermo.")
                break

            web, gql = got.get("web"), got.get("gql")
            if got.get("web_src") == "passivo":
                src_stat["web_passivo"] += 1
            elif got.get("web_src") == "fetch":
                src_stat["web_fetch"] += 1
            elif got.get("web_http") == 400:
                src_stat["web_400"] += 1
            elif got.get("web_http"):
                src_stat["web_altro"] += 1
            if gql:
                src_stat["gql_passivo"] += 1
            if not web and not gql:
                src_stat["nessuna"] += 1

            p_web, p_gql = professional(web), professional(gql)
            # Il gate userebbe la sorgente che c'e', preferendo web.
            p = p_web if p_web is not None else p_gql

            for c in mancanti_gql(gql):
                lacune[c] = lacune.get(c, 0) + 1

            esito = "OK " if (p is True) == bool(da_info) or (da_info and p) else "?? "
            if da_info and p is not True:
                esito = "ROTTO"
            print(f"[{i+1:>2}/{len(targets)}] {esito} @{uname:<26} "
                  f"info={'SI' if da_info else 'no':<3} prof={p} "
                  f"(web={p_web} gql={p_gql}) src={got.get('web_src') or got.get('web_http') or '-'}")
            righe.append((uname, da_info, p, p_web, p_gql))

            if i < len(targets) - 1:
                await asyncio.sleep(6.0)
    finally:
        await session.close()

    print("\n" + "=" * 72)
    letti = len(righe)
    if not letti:
        print("Nessun profilo letto.")
        return

    con_info = [r for r in righe if r[1]]
    rotti = [r for r in con_info if r[2] is not True]
    prof_tot = sum(1 for r in righe if r[2] is True)
    illeggibili = [r for r in righe if r[2] is None]

    print(f"1. GATE   — profili con contatto da /info/: {len(con_info)}")
    print(f"            di questi NON professional (= contatto perso): {len(rotti)}")
    if rotti:
        print("            ROTTI: " + ", ".join(f"@{r[0]}" for r in rotti))
    else:
        print("            nessuno. Il gate non perde contatti su questo campione.")
    print(f"            profili senza segnale leggibile: {len(illeggibili)}")

    print(f"\n2. RESA   — professional: {prof_tot}/{letti} ({prof_tot*100//letti}%)")
    print(f"            /info/ risparmiate dal gate: {letti - prof_tot}/{letti} "
          f"({(letti-prof_tot)*100//letti}%)")

    print(f"\n3. SORGENTI su {letti} profili")
    for k, v in src_stat.items():
        print(f"            {k:<14} {v:>3}  ({v*100//letti}%)")
    print(f"            campi che mancano al payload GraphQL: "
          f"{json.dumps(lacune, ensure_ascii=False) if lacune else 'nessuno'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python -m scripts.probe_professional_gate_30 <account_id> [quanti]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 30))
