import asyncio, sys
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.campaign import Campaign
from app.models.global_contact import GlobalContact
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
        empty_bio = nonempty_zero = 0
        samples = []
        for c in contacts:
            det = score_lead(c, rules, pass_threshold=profile.pass_threshold, reject_threshold=5)
            if det.score == 0:
                bio = (c.biography or "").strip()
                if not bio and not (c.full_name or "").strip():
                    empty_bio += 1
                else:
                    nonempty_zero += 1
                    if len(samples) < 40:
                        samples.append((c.username, c.full_name, bio[:90]))
        print(f"score-0 total empty(no bio & no name): {empty_bio}")
        print(f"score-0 with some bio/name (keyword-miss candidates): {nonempty_zero}\n")
        for un, fn, bio in samples:
            print(f"  @{un} | {fn!r} | {bio!r}")
asyncio.run(main())
