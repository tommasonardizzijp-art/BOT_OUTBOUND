"""Probe LIVE del fallback GraphQL (NON un test pytest).

Uso (a mano, con un account loggato):
    # IMPORTANTE: NON impostare PLAYWRIGHT_BROWSERS_PATH. I profili account sono
    # nati con chromium-1208 (C:\\...\\ms-playwright); puntare a D:\\dev\\.playwright-browsers
    # (build 1228/1234) fa un upgrade IRREVERSIBILE del profilo + fingerprint diverso
    # = sessione corrotta. Lascia il default (C: chromium-1208, quello che usa il bot).
    # I profili loggati stanno nel worktree PRINCIPALE: se giri da un worktree, punta
    #   BROWSER_PROFILES_DIR="D:/BOT OUTBOUND/backend/data/browser_profiles"
    # e copia il .env del progetto nel CWD.
    BROWSER_PROFILES_DIR="D:/BOT OUTBOUND/backend/data/browser_profiles" \\
    python -m scripts.probe_graphql_fallback <account_id> user1 user2 ...

Per ogni username apre il profilo, esegue _capture_web_profile_info e stampa da
DOVE e' arrivato il dato (web_profile_info vs GraphQL fallback) e i campi chiave.
Serve a confermare che il fallback recupera davvero i 23 profili business falliti,
NON solo l'uno gia' verificato nell'audit. Nessuna scrittura su DB.
"""
import asyncio
import sys

from app.browser.context_manager import BrowserSession
from app.services.browser_bio import (
    _capture_web_profile_info, graphql_user_to_web_shape, web_user_to_shim,
)
from app.utils.contact_extract import extract_contacts


async def main(account_id: str, usernames: list[str]) -> None:
    session = BrowserSession(account_id, headless=False)
    await session.open()
    await session.page.ensure_logged_in(account_id, allow_login=False)
    raw_page = await session.page._get_page()

    ok_web = ok_gql = fail = 0
    try:
        for uname in usernames:
            user = await _capture_web_profile_info(raw_page, uname, timeout_s=8.0)
            if user is None:
                print(f"[FAIL ] @{uname}: nessun dato (ne' web ne' GraphQL)")
                fail += 1
                continue
            if isinstance(user, dict) and user.get("__status"):
                print(f"[FAIL ] @{uname}: HTTP {user['__status']} (rate-limit, non mascherato)")
                fail += 1
                continue
            # 'id' presente perche' normalizzato: distinguo web da gql guardando se
            # e' arrivato con la forma flat originale non e' possibile qui (gia'
            # normalizzato), quindi ristampo solo i campi finali.
            shim = web_user_to_shim(user)
            c = extract_contacts(shim)
            print(f"[OK   ] @{uname}: pk={shim.pk} followers={shim.follower_count} "
                  f"bio_len={len(shim.biography or '')} email={c.email}")
            ok_web += 1  # (il conteggio web-vs-gql preciso si legge dai log INFO di _capture_web_profile_info)
            await asyncio.sleep(6.0)  # ritmo umano tra profili
    finally:
        await session.close()

    print(f"\n== Riepilogo: {ok_web} risolti, {fail} falliti su {len(usernames)} ==")
    print("(cerca 'uso fallback GraphQL passivo' nei log INFO per contare i recuperi GraphQL)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python -m scripts.probe_graphql_fallback <account_id> user1 [user2 ...]")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
