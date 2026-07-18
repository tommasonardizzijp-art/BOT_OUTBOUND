"""Sweep one-off dell'inbox DM per una campagna dm_threads:

1. pagina l'inbox con il pattern collaudato di inbox_source.fetch_inbox_page
   (pagine da 20, parametri identici all'app reale, delay 10-40s tra pagine
   + pausa lunga occasionale — stessi valori di config del listing inbox);
2. per ogni thread 1-a-1: se l'altro utente ha scritto ALMENO un messaggio
   (tra gli ultimi 10 del thread) -> "replied";
3. aggiorna i Follower della campagna:
   - replied            -> status=replied (esce dalla lista d'invio)
   - non-replied pending -> status=bio_scraped (pronto per l'invio in
     template mode: la Fase Bio non serve, nome/username li abbiamo gia')
   - thread nuovo non in lista -> crea il Follower (contatti aggiunti dopo
     l'ultima estrazione lista)
4. salva uno snapshot JSON di audit in backend/data/ e stampa il report.

NON invia nulla e NON avvia la campagna: prepara solo la lista.

Uso (dal folder backend, bot fermo o acceso e' indifferente ma l'account
inbox non deve essere in uso da altri job):
    ./venv/Scripts/python.exe scripts/sweep_inbox_replies.py --campaign borderline
    opzioni: --max-pages 30  --daily-limit 100  --dry-run  --account <username>
"""
import argparse
import asyncio
import json
import os
import random
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.account import InstagramAccount
from app.models.campaign import Campaign
from app.models.campaign_account import CampaignAccount
from app.models.follower import Follower, FollowerStatus
from app.utils.instagrapi_client import login as _login
from app.utils.roles import is_inbox
from app.services.inbox_source import fetch_inbox_page, extract_thread_participant, _as_users


def _thread_items(raw_thread) -> list:
    if isinstance(raw_thread, dict):
        items = raw_thread.get("items") or []
        return items if isinstance(items, list) else []
    return getattr(raw_thread, "messages", []) or []


def _item_user_id(item) -> int | None:
    raw = item.get("user_id") if isinstance(item, dict) else getattr(item, "user_id", None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _item_type(item) -> str:
    if isinstance(item, dict):
        return str(item.get("item_type") or "?")
    return str(getattr(item, "item_type", "?"))


def _full_name_of(raw_thread, pk: int) -> str | None:
    if not isinstance(raw_thread, dict):
        return None
    for u in raw_thread.get("users") or []:
        try:
            if int(u.get("pk")) == pk:
                name = (u.get("full_name") or "").strip()
                return name or None
        except (TypeError, ValueError):
            continue
    return None


async def _pick_account(campaign: Campaign, db, username_override: str | None) -> InstagramAccount | None:
    if username_override:
        return (await db.execute(
            select(InstagramAccount).where(InstagramAccount.username == username_override)
        )).scalar_one_or_none()
    rows = (await db.execute(
        select(CampaignAccount, InstagramAccount)
        .join(InstagramAccount, InstagramAccount.id == CampaignAccount.account_id)
        .where(CampaignAccount.campaign_id == campaign.id)
    )).all()
    inbox_accounts = [acc for ca, acc in rows if is_inbox(ca.role)]
    if len(inbox_accounts) == 1:
        return inbox_accounts[0]
    if not inbox_accounts and len(rows) == 1:
        return rows[0][1]
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True, help="substring del nome campagna (match unico)")
    ap.add_argument("--account", default=None, help="username account inbox (default: ruolo inbox della campagna)")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--daily-limit", type=int, default=None, help="se passato, imposta campaigns.daily_limit")
    ap.add_argument("--dry-run", action="store_true", help="nessuna scrittura DB (solo sweep + report + snapshot)")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        camps = (await db.execute(
            select(Campaign).where(Campaign.name.ilike(f"%{args.campaign}%"))
        )).scalars().all()
        if len(camps) != 1:
            print(f"[X] match campagna non univoco per '{args.campaign}': {[c.name for c in camps]}")
            return
        campaign = camps[0]
        print(f"[OK] campagna: '{campaign.name}' (id={campaign.id}, status={campaign.status.value}, "
              f"scrape_mode={campaign.scrape_mode})")

        account = await _pick_account(campaign, db, args.account)
        if account is None:
            print("[X] account inbox non determinabile: passa --account <username>")
            return
        print(f"[..] login @{account.username} (status={account.status.value}, proxy={account.proxy or 'nessuno'})")
        try:
            client = await _login(account, db, skip_gql_verify=True)
        except Exception as e:
            print(f"[X] login fallito ({type(e).__name__}): {e}")
            return
        own_pk = int(client.user_id)

        # followers gia' in lista, indicizzati per pk
        existing = {
            f.ig_user_id: f
            for f in (await db.execute(
                select(Follower).where(Follower.campaign_id == campaign.id)
            )).scalars().all()
        }
        print(f"[OK] follower gia' in lista: {len(existing)}")

        # ── sweep paginato con pacing collaudato ──────────────────────────
        seen: dict[int, dict] = {}
        cursor = None
        pages = 0
        while pages < args.max_pages:
            try:
                threads, cursor, has_older = await asyncio.to_thread(fetch_inbox_page, client, cursor)
            except Exception as e:
                es = str(e).lower()
                tag = "429/RATE" if ("429" in es or "too many" in es or "rate" in es) else type(e).__name__
                print(f"[X] pagina {pages + 1} fallita ({tag}): {e} — mi fermo qui, dati parziali validi")
                break
            pages += 1
            for t in threads:
                p = extract_thread_participant(_as_users(t), own_pk)
                if p is None:
                    continue
                pk, username = p
                items = _thread_items(t)
                their_items = [it for it in items if _item_user_id(it) == pk]
                entry = seen.setdefault(pk, {
                    "username": username,
                    "full_name": _full_name_of(t, pk),
                    "replied": False,
                    "reply_item_types": [],
                })
                if their_items:
                    entry["replied"] = True
                    entry["reply_item_types"] = sorted({_item_type(it) for it in their_items})
            replied_so_far = sum(1 for v in seen.values() if v["replied"])
            print(f"[..] pagina {pages}: thread visti {len(seen)} (di cui replied {replied_so_far}), "
                  f"has_older={has_older}")
            if not has_older or not cursor:
                print("[OK] inbox esaurita")
                break
            if pages >= args.max_pages:
                break
            delay = random.uniform(
                settings.inbox_api_page_delay_min_seconds,
                settings.inbox_api_page_delay_max_seconds,
            )
            if random.random() < settings.inbox_long_pause_probability:
                delay += random.uniform(
                    settings.inbox_long_pause_min_seconds,
                    settings.inbox_long_pause_max_seconds,
                )
            await asyncio.sleep(delay)

        # ── snapshot audit ────────────────────────────────────────────────
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        snap_path = os.path.join(data_dir, f"sweep_inbox_{ts}.json")
        with open(snap_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"campaign_id": campaign.id, "account": account.username, "pages": pages,
                 "threads": {str(k): v for k, v in seen.items()}},
                fh, ensure_ascii=True, indent=1,
            )

        # ── applicazione al DB ────────────────────────────────────────────
        n_replied_upd = n_promoted = n_new = n_new_replied = n_untouched = 0
        for pk, info in seen.items():
            f = existing.get(pk)
            if f is not None:
                if info["replied"]:
                    if f.status not in (FollowerStatus.replied, FollowerStatus.skipped):
                        if not args.dry_run:
                            f.status = FollowerStatus.replied
                        n_replied_upd += 1
                elif f.status == FollowerStatus.pending:
                    if not args.dry_run:
                        f.status = FollowerStatus.bio_scraped
                    n_promoted += 1
                else:
                    n_untouched += 1
            else:
                if not args.dry_run:
                    db.add(Follower(
                        id=str(uuid.uuid4()),
                        campaign_id=campaign.id,
                        ig_user_id=pk,
                        username=info["username"],
                        full_name=info["full_name"],
                        status=FollowerStatus.replied if info["replied"] else FollowerStatus.bio_scraped,
                    ))
                n_new += 1
                if info["replied"]:
                    n_new_replied += 1
        unseen = [f for pk, f in existing.items() if pk not in seen]
        if args.daily_limit is not None and not args.dry_run:
            campaign.daily_limit = args.daily_limit
        if not args.dry_run:
            await db.commit()

        total_replied = sum(1 for v in seen.values() if v["replied"])
        sendable = (len(seen) - total_replied)
        print("=" * 64)
        print(f"REPORT SWEEP {'(DRY-RUN, nessuna scrittura)' if args.dry_run else ''}")
        print(f"  pagine inbox lette              : {pages}")
        print(f"  thread 1-a-1 visti              : {len(seen)}")
        print(f"  hanno risposto (esclusi)        : {total_replied}")
        print(f"  da contattare (visti nel sweep) : {sendable}")
        print(f"  -- gia' in lista -> replied     : {n_replied_upd}")
        print(f"  -- gia' in lista -> promossi    : {n_promoted}")
        print(f"  -- gia' in lista, stato lasciato: {n_untouched}")
        print(f"  -- nuovi aggiunti alla lista    : {n_new} (di cui replied {n_new_replied})")
        print(f"  in lista ma NON visti nel sweep : {len(unseen)} (thread oltre le pagine lette)")
        if unseen[:10]:
            print(f"     esempi: {', '.join('@' + f.username for f in unseen[:10])}")
        if args.daily_limit is not None:
            print(f"  daily_limit campagna            : {args.daily_limit}{' (dry-run: NON scritto)' if args.dry_run else ''}")
        print(f"  snapshot audit                  : {snap_path}")
        print("=" * 64)


asyncio.run(main())
