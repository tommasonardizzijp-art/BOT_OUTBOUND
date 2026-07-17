"""Probe fine-lista Fase Lista: capisce se il "Fine lista (6755)" su una pagina
da ~10.8k follower e' un limite reale di IG o un glitch del cursore — SENZA
rescan di 270 pagine.

Fase A (5 richieste): pagina dall'inizio con count=list_page_size e misura
  - quanti utenti per RISPOSTA rende IG (il tetto reale, atteso 25)
  - il formato di next_max_id (numerico = offset saltabile / opaco)
  - overlap col DB (gia' presenti = ordine stabile, testa gia' presa;
    molti nuovi = follower nuovi hanno spostato la catena)

Fase B (2 richieste, solo se il cursore e' un offset numerico): salta
direttamente a max_id oltre il punto dove la lista si e' chiusa (6755).
  - utenti tornati + next_max_id presente  -> IG serve oltre: era glitch cursore
  - vuoto / niente cursore                 -> fine reale della lista servita

Extra (1 richiesta): user_info del target per il follower_count dichiarato
dall'API (il 10.8k mostrato sul profilo puo' includere account rimossi).

Totale: max 8 richieste, delay 8-15s tra una e l'altra, NESSUNA scrittura DB.

Uso (dal folder backend):
    ./venv/Scripts/python.exe scripts/probe_list_tail.py <account_username> [campaign_id]
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.campaign import Campaign
from app.models.follower import Follower
from app.utils.instagrapi_client import login as _login

DEFAULT_CAMPAIGN = "d39d304c-45bf-4f22-b450-1b8d0545eb5b"  # shop survivor
PAGES_FROM_START = 5
JUMP_OFFSETS = [6800, 9500]  # oltre il punto di "Fine lista (6755)"


def _fetch_page(client, user_id, count, max_id):
    """UNA richiesta cruda friendships/followers — nessun loop interno instagrapi."""
    result = client.private_request(
        f"friendships/{user_id}/followers/",
        params={
            "max_id": max_id or "",
            "count": count,
            "rank_token": client.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        },
    )
    users = result.get("users") or []
    return users, result.get("next_max_id")


async def _overlap(db, campaign_id, users):
    """Quanti dei pk tornati sono gia' nel DB della campagna."""
    pks = [int(u["pk"]) for u in users if u.get("pk")]
    if not pks:
        return 0
    rows = await db.scalar(
        select(func.count(Follower.id)).where(
            Follower.campaign_id == campaign_id,
            Follower.ig_user_id.in_(pks),
        )
    )
    return rows or 0


async def _delay():
    d = random.uniform(8, 15)
    print(f"     ... delay {d:.0f}s")
    await asyncio.sleep(d)


async def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/probe_list_tail.py <account_username> [campaign_id]")
        return
    username = sys.argv[1]
    campaign_id = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CAMPAIGN

    async with AsyncSessionLocal() as db:
        campaign = (await db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )).scalar_one_or_none()
        if campaign is None:
            print(f"[X] campagna {campaign_id} non trovata")
            return
        if not campaign.target_user_id:
            print("[X] campagna senza target_user_id risolto")
            return
        in_db = await db.scalar(
            select(func.count(Follower.id)).where(Follower.campaign_id == campaign_id)
        ) or 0
        print(f"[..] campagna '{campaign.name}' target=@{campaign.target_username} "
              f"(uid {campaign.target_user_id}) — follower in DB: {in_db}")

        acct = (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == username)
        )).scalar_one_or_none()
        if acct is None:
            print(f"[X] account @{username} non trovato")
            return
        print(f"[..] login @{acct.username} (status={acct.status.value}, proxy={acct.proxy or 'nessuno'})")
        try:
            client = await _login(acct, db)
        except Exception as e:
            print(f"[X] login fallito ({type(e).__name__}): {e}")
            return

        uid = campaign.target_user_id
        count = settings.list_page_size

        # ── Extra: follower_count dichiarato dall'API ──
        api_count = None
        try:
            info = await asyncio.to_thread(client.user_info_v1, str(uid))
            api_count = info.follower_count
            print(f"[OK] follower_count secondo l'API: {api_count}")
        except Exception as e:
            print(f"[!] user_info fallita ({type(e).__name__}): {e} — proseguo")
        await _delay()

        # ── Fase A: pagine dall'inizio ──
        print(f"\n=== FASE A: {PAGES_FROM_START} pagine da inizio lista (count={count}) ===")
        max_id = None
        cursor_numeric = True
        total_seen = 0
        for i in range(1, PAGES_FROM_START + 1):
            try:
                users, next_max_id = await asyncio.to_thread(_fetch_page, client, uid, count, max_id)
            except Exception as e:
                es = str(e).lower()
                tag = "429/RATE" if ("429" in es or "too many" in es or "wait" in es) else type(e).__name__
                print(f"[X] pagina {i} fallita ({tag}): {e} — STOP probe")
                return
            dup = await _overlap(db, campaign_id, users)
            total_seen += len(users)
            cur_repr = repr(next_max_id)[:60] if next_max_id else "ASSENTE"
            if next_max_id and not str(next_max_id).isdigit():
                cursor_numeric = False
            print(f"[A{i}] utenti/risposta: {len(users):>3} | gia' in DB: {dup:>3}/{len(users)} "
                  f"| next_max_id: {cur_repr}")
            if not next_max_id:
                print(">>> lista chiusa da IG gia' in testa?! (anomalo)")
                break
            max_id = next_max_id
            if i < PAGES_FROM_START:
                await _delay()

        # ── Fase B: salto oltre 6755 (solo se cursore = offset numerico) ──
        if cursor_numeric:
            print(f"\n=== FASE B: salto diretto oltre il punto di fine-lista ===")
            for off in JUMP_OFFSETS:
                await _delay()
                try:
                    users, next_max_id = await asyncio.to_thread(_fetch_page, client, uid, count, str(off))
                except Exception as e:
                    es = str(e).lower()
                    tag = "429/RATE" if ("429" in es or "too many" in es or "wait" in es) else type(e).__name__
                    print(f"[X] salto a {off} fallito ({tag}): {e} — STOP probe")
                    return
                dup = await _overlap(db, campaign_id, users)
                cur_repr = repr(next_max_id)[:60] if next_max_id else "ASSENTE"
                print(f"[B@{off}] utenti: {len(users):>3} | gia' in DB: {dup:>3}/{len(users)} "
                      f"| next_max_id: {cur_repr}")
                if users and next_max_id:
                    print(f">>> IG SERVE oltre {off}: il 'Fine lista (6755)' era un glitch di cursore")
                elif not users:
                    print(f">>> lista VUOTA a offset {off}: coerente con fine reale della lista servita")
        else:
            print("\n[!] cursore NON numerico (opaco) — salto diretto impossibile, Fase B saltata")

        print("\n" + "=" * 60)
        print(f"RIEPILOGO: tetto/risposta atteso 25 | follower API: {api_count} | in DB: {in_db}")
        print("FATTO — nessuna scrittura DB, sessione non modificata.")


asyncio.run(main())
