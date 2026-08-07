"""Probe LIVE del gate 'professional' (NON un test pytest).

Domanda a cui risponde: possiamo sapere PRIMA di chiamare /info/ se un profilo
puo' avere contatti pubblici, leggendo solo il payload che scarichiamo comunque?

Per ogni username:
  1. apre il profilo e cattura web_profile_info (passivo o fetch in-page) e la
     risposta GraphQL passiva, tenendole SEPARATE (a differenza del codice di
     produzione, che le fonde) — cosi' si vede QUALE delle due porta il segnale;
  2. stampa i campi candidati al gate (is_business_account, account_type, ...)
     da entrambe le sorgenti;
  3. chiama /info/ e dice se ha prodotto un contatto.

L'incrocio (2)x(3) e' la prova: se ogni profilo che rende un contatto e' marcato
professional, il gate e' sicuro. Se anche UNO rende un contatto senza esserlo,
il gate perde contatti e va scartato.

Uso (dalla cartella backend, con un account gia' loggato):
    # NON impostare PLAYWRIGHT_BROWSERS_PATH: i profili sono nati con chromium-1208
    # e puntare altrove fa un upgrade IRREVERSIBILE del profilo.
    ./venv/Scripts/python.exe -m scripts.probe_professional_gate <account_id> user1 user2 ...

Nessuna scrittura su DB.
"""
import asyncio
import json
import sys

from app.browser.context_manager import BrowserSession
from app.services.browser_bio import (
    WEB_APP_ID, _fetch_public_contact_inpage, graphql_user_to_web_shape,
    web_user_to_shim,
)

# Chiavi che potrebbero portare il segnale professional. Le cerchiamo per nome
# in ENTRAMBE le sorgenti invece di assumere quale esista.
CANDIDATE_KEYS = (
    "is_business_account",
    "is_professional_account",
    "is_business",
    "account_type",
    "business_category_name",
    "category_name",
    "category",
    "category_enum",
    "should_show_category",
    "is_verified",
    "is_private",
)

WEB_PROFILE_PATH = "/api/v1/users/web_profile_info/"
GRAPHQL_PATH = "/api/graphql"


async def capture_both(raw_page, username: str, timeout_s: float = 9.0) -> dict:
    """Come _capture_web_profile_info ma tiene web e GraphQL separati e segnala
    se il payload web e' arrivato passivo o e' servito il fetch esplicito."""
    got: dict = {}

    async def on_response(resp):
        try:
            if WEB_PROFILE_PATH in resp.url and resp.status == 200 and "web" not in got:
                u = (((await resp.json()) or {}).get("data") or {}).get("user")
                if u:
                    got["web"] = u
                    got["web_source"] = "passivo"
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
            # Fetch esplicito: stessa forma del codice di produzione.
            res = await raw_page.evaluate(
                """async (args) => {
                    const [u, appId] = args;
                    const r = await fetch(
                        `/api/v1/users/web_profile_info/?username=${encodeURIComponent(u)}`,
                        { headers: { 'x-ig-app-id': appId }, credentials: 'include' });
                    if (!r.ok) return { __status: r.status };
                    return await r.json();
                }""",
                [username, WEB_APP_ID],
            )
            if isinstance(res, dict) and res.get("__status"):
                got["web_error"] = res["__status"]
            else:
                u = (((res or {}).get("data") or {}).get("user"))
                if u:
                    got["web"] = u
                    got["web_source"] = "fetch esplicito"
        return got
    finally:
        try:
            raw_page.remove_listener("response", on_response)
        except Exception:
            pass


def campi(payload: dict | None) -> dict:
    if not payload:
        return {}
    return {k: payload.get(k) for k in CANDIDATE_KEYS if k in payload}


async def main(account_id: str, usernames: list[str]) -> None:
    session = BrowserSession(account_id, headless=False)
    await session.open()
    await session.page.ensure_logged_in(account_id, allow_login=False)
    raw_page = await session.page._get_page()

    righe = []
    try:
        for i, uname in enumerate(usernames):
            print(f"\n{'=' * 70}\n@{uname}")
            got = await capture_both(raw_page, uname)

            url_ora = raw_page.url
            if "challenge" in url_ora or "warning" in url_ora or "checkpoint" in url_ora:
                print(f"  !! INTERSTIZIALE: {url_ora[:120]}")
                print("  !! Interrompo: l'account e' dietro un blocco, i dati non sarebbero validi.")
                break

            web, gql = got.get("web"), got.get("gql")
            if got.get("web_error"):
                print(f"  web_profile_info: HTTP {got['web_error']}")
            print(f"  sorgente web: {got.get('web_source', 'ASSENTE')}"
                  f" | graphql passivo: {'si' if gql else 'no'}")
            print(f"  campi da web_profile_info: {json.dumps(campi(web), ensure_ascii=False)}")
            print(f"  campi da graphql        : {json.dumps(campi(gql), ensure_ascii=False)}")

            # Chiavi mai viste prima, per non perdere un segnale che non stiamo cercando.
            for etichetta, src in (("web", web), ("gql", gql)):
                if src:
                    extra = sorted(k for k in src
                                   if any(t in k.lower() for t in
                                          ("business", "professional", "category", "account_type"))
                                   and k not in CANDIDATE_KEYS)
                    if extra:
                        print(f"  altre chiavi {etichetta}: {extra}")

            base = web or (graphql_user_to_web_shape(gql) if gql else None)
            pk = web_user_to_shim(base).pk if base else None

            info = await _fetch_public_contact_inpage(raw_page, pk) if pk else None
            if isinstance(info, dict) and info.get("__rate_limited"):
                print(f"  /info/ : RATE-LIMITED HTTP {info['__rate_limited']} — mi fermo qui.")
                righe.append((uname, campi(web), campi(gql), "RATE_LIMIT"))
                break
            contatti = {k: v for k, v in (info or {}).items() if v}
            print(f"  /info/ : {json.dumps(contatti, ensure_ascii=False) if contatti else 'NIENTE'}")

            righe.append((uname, campi(web), campi(gql), "CONTATTO" if contatti else "vuoto"))

            if i < len(usernames) - 1:
                await asyncio.sleep(7.0)  # ritmo umano tra profili
    finally:
        await session.close()

    print(f"\n\n{'=' * 70}\nINCROCIO — il gate regge solo se ogni CONTATTO e' marcato professional\n")
    print(f"{'profilo':<28} {'esito /info/':<12} segnale professional")
    for uname, cweb, cgql, esito in righe:
        src = cweb or cgql
        seg = {k: v for k, v in src.items()
               if k in ("is_business_account", "is_professional_account", "is_business", "account_type")}
        print(f"{uname:<28} {esito:<12} {json.dumps(seg, ensure_ascii=False)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python -m scripts.probe_professional_gate <account_id> user1 [user2 ...]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
