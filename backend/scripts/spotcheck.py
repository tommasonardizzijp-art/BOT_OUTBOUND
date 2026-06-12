import asyncio, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.lead_qualification import LeadQualificationRun, LeadTargetProfile
from app.schemas.lead_qualification import LeadQualificationFilters
from app.services.lead_qualification import candidate_select, score_lead
from app.services.lead_qualification_rules import safe_json_loads

async def main():
    async with AsyncSessionLocal() as db:
        camp = (await db.execute(select(Campaign).where(Campaign.name.ilike("%survivor%")))).scalars().first()
        runs = (await db.execute(select(LeadQualificationRun).order_by(LeadQualificationRun.created_at.desc()))).scalars().all()
        src = next(r for r in runs if camp.id in (safe_json_loads(r.filters, {}).get("campaign_ids") or []))
        profile = await db.get(LeadTargetProfile, src.target_profile_id)
        rules = safe_json_loads(profile.compiled_rules, {})
        filters = LeadQualificationFilters(campaign_ids=[camp.id], date_to=safe_json_loads(src.filters, {}).get("date_to"), skip_existing_same_rules=False, max_leads=5000)
        contacts = (await db.execute(candidate_select(filters))).scalars().all()
        term_freq = Counter()
        m, a = [], []
        for c in contacts:
            det = score_lead(c, rules, pass_threshold=10, reject_threshold=0)
            if det.status == "match":
                for s in det.matched_signals:
                    if s["kind"] in ("positive", "strong"):
                        term_freq[s["term"]] += 1
                if len(m) < 22:
                    m.append((det.score, c.username, (c.biography or "")[:60], [s["term"] for s in det.matched_signals]))
            elif det.status == "ambiguous" and len(a) < 12:
                a.append((det.score, c.username, (c.biography or "")[:60], [s["term"] for s in det.matched_signals]))
        print("=== TOP terms triggering MATCH ===")
        for t, n in term_freq.most_common(25):
            print(f"  {n:4d}  {t}")
        print("\n=== sample MATCH ===")
        for sc, u, b, ts in m:
            print(f"  [{sc}] @{u}: {b!r} <- {ts}")
        print("\n=== sample AMBIGUOUS (->AI) ===")
        for sc, u, b, ts in a:
            print(f"  [{sc}] @{u}: {b!r} <- {ts}")
asyncio.run(main())
